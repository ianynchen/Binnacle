"""Integration tests for PostgresStore read primitives (needs a live Postgres; see
conftest.pg_dsn). Seeds the REQUIREMENTS.md §7 narrative — an agent records a
backoff decision, supersedes it with a batching decision, records a general
(unscoped) policy and a differently-scoped decision, the gate declines and
archives/discards others, and a later decision supplements the survivor — then
exercises every read method against that one coherent fixture.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from binnacle.adapters.postgres_store import PostgresStore
from binnacle.domain.errors import DecisionNotFound
from binnacle.domain.models import Actor, CompactDecision, Decision, Ref

HUMAN = Actor(kind="human", id="alice")
AGENT = Actor(kind="agent", id="meridian/s1")
ENGINE = Actor(kind="engine", id="binnacle")


def _decision(**overrides: Any) -> Decision:
    base: dict[str, Any] = {
        "decision_id": uuid4(),
        "domain": "architecture",
        "tier": "short_term",
        "status": "current",
        "scenario": "how should transient ingestion failures be handled?",
        "outcome": "retry with exponential backoff, capped at 3 attempts",
        "reasoning": "avoids thundering herd on recovery",
        "source": "meridian",
        "recorded_by": AGENT,
        "recorded_at": datetime.now(UTC),
    }
    base.update(overrides)
    return Decision(**base)


def _vector(dim: int, val: float, dim2: int | None = None, val2: float = 0.0) -> list[float]:
    v = [0.0] * 768
    v[dim] = val
    if dim2 is not None:
        v[dim2] = val2
    return v


@pytest.fixture()
async def store(pg_dsn: str, scratch_schema: str) -> AsyncIterator[PostgresStore]:
    s = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=768)
    await s.migrate()
    async with s.transaction() as tx:
        await s.upsert_domain(
            tx,
            "architecture",
            "architecture decisions",
            True,
            HUMAN.as_str(),
            "domain_created",
            None,
        )
        await s.upsert_domain(
            tx, "product", "product decisions", True, HUMAN.as_str(), "domain_created", None
        )
    yield s
    await s.aclose()


@dataclass
class Narrative:
    """Ids of the §7 narrative fixture's decisions, plus the moment it was seeded."""

    backoff: UUID
    batching: UUID
    general: UUID
    other_component: UUID
    expired: UUID
    archived: UUID
    discarded: UUID
    supplement: UUID
    now: datetime


@pytest.fixture()
async def narrative(store: PostgresStore) -> Narrative:
    now = datetime.now(UTC)

    # 7.1: the moment of recording — a backoff decision scoped to one component.
    backoff = _decision(recorded_at=now - timedelta(days=10), confidence=0.8)
    async with store.transaction() as tx:
        await store.insert_decision(tx, backoff, "h-backoff")
        await store.insert_refs(
            tx,
            backoff.decision_id,
            [
                Ref(role="subject", kind="component", identifier="portolan-ingest"),
                Ref(role="evidence", kind="session", identifier="s-1"),
            ],
        )
        await store.apply_transition(
            tx, backoff.decision_id, "recorded", AGENT.as_str(), None, None, "current"
        )

    # 7.2: the working life — batching supersedes backoff, same subject.
    batching = _decision(
        recorded_at=now - timedelta(days=9),
        outcome="batch ingestion; retries unnecessary",
        confidence=0.75,
    )
    async with store.transaction() as tx:
        await store.insert_decision(tx, batching, "h-batching")
        await store.insert_refs(
            tx,
            batching.decision_id,
            [Ref(role="subject", kind="component", identifier="portolan-ingest")],
        )
        await store.apply_transition(
            tx, batching.decision_id, "recorded", AGENT.as_str(), None, None, "current"
        )
        await store.insert_link(tx, batching.decision_id, backoff.decision_id, "SUPERSEDES")
        await store.apply_transition(
            tx,
            backoff.decision_id,
            "superseded",
            AGENT.as_str(),
            "superseded by batching",
            {"target": str(batching.decision_id)},
            "superseded",
        )

    # A general (unscoped) policy — FR-6.1's "no subject refs at all" case.
    general = _decision(
        recorded_at=now - timedelta(days=8),
        scenario="what log format should services use?",
        outcome="all services log structured JSON",
        source="portolan",
    )
    async with store.transaction() as tx:
        await store.insert_decision(tx, general, "h-general")
        await store.apply_transition(
            tx, general.decision_id, "recorded", HUMAN.as_str(), None, None, "current"
        )

    # Scoped to a *different* subject — must not match a portolan-ingest query.
    other_component = _decision(
        recorded_at=now - timedelta(days=7),
        scenario="how should downstream call failures be handled?",
        outcome="use a circuit breaker for downstream calls",
        source="portolan",
    )
    async with store.transaction() as tx:
        await store.insert_decision(tx, other_component, "h-other")
        await store.insert_refs(
            tx,
            other_component.decision_id,
            [Ref(role="subject", kind="component", identifier="other-service")],
        )
        await store.apply_transition(
            tx, other_component.decision_id, "recorded", HUMAN.as_str(), None, None, "current"
        )

    # A temporary waiver already expired relative to "now" (FR-6.1 as-of default).
    expired = _decision(
        recorded_at=now - timedelta(days=6),
        scenario="should the legacy client get a grace period?",
        outcome="temporary waiver for legacy client",
        valid_from=now - timedelta(days=6),
        valid_until=now - timedelta(days=1),
    )
    async with store.transaction() as tx:
        await store.insert_decision(tx, expired, "h-expired")
        await store.apply_transition(
            tx, expired.decision_id, "recorded", HUMAN.as_str(), None, None, "current"
        )

    # 7.5: old age — archived by the clock sweep.
    archived = _decision(
        recorded_at=now - timedelta(days=100),
        scenario="tabs or spaces?",
        outcome="use tabs not spaces",
    )
    async with store.transaction() as tx:
        await store.insert_decision(tx, archived, "h-archived")
        await store.apply_transition(
            tx, archived.decision_id, "recorded", AGENT.as_str(), None, None, "current"
        )
        await store.apply_transition(
            tx, archived.decision_id, "archived", ENGINE.as_str(), None, None, "archived"
        )

    # 7.2: "a malformed duplicate ... gets discarded."
    discarded = _decision(
        recorded_at=now - timedelta(days=5),
        scenario="duplicate?",
        outcome="duplicate log entry",
    )
    async with store.transaction() as tx:
        await store.insert_decision(tx, discarded, "h-discarded")
        await store.apply_transition(
            tx, discarded.decision_id, "recorded", AGENT.as_str(), None, None, "current"
        )
        await store.apply_transition(
            tx,
            discarded.decision_id,
            "discarded",
            AGENT.as_str(),
            "malformed duplicate",
            None,
            "discarded",
        )

    # 7.4: "a new decision supplements it."
    supplement = _decision(
        recorded_at=now - timedelta(days=1),
        scenario="how should queue-fed ingestion handle poison messages?",
        outcome="queue-fed ingestion additionally uses dead-lettering",
    )
    async with store.transaction() as tx:
        await store.insert_decision(tx, supplement, "h-supplement")
        await store.apply_transition(
            tx, supplement.decision_id, "recorded", HUMAN.as_str(), None, None, "current"
        )
        await store.insert_link(tx, supplement.decision_id, batching.decision_id, "SUPPLEMENTS")

    return Narrative(
        backoff=backoff.decision_id,
        batching=batching.decision_id,
        general=general.decision_id,
        other_component=other_component.decision_id,
        expired=expired.decision_id,
        archived=archived.decision_id,
        discarded=discarded.decision_id,
        supplement=supplement.decision_id,
        now=now,
    )


class TestGetDecisionAndGetMany:
    async def test_get_decision_hydrates_refs_and_declared_supersedes(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        d = await store.get_decision(narrative.batching)
        assert d is not None
        assert d.decision_id == narrative.batching
        assert narrative.backoff in d.supersedes
        assert any(r.identifier == "portolan-ingest" for r in d.refs)

    async def test_get_decision_missing_returns_none(self, store: PostgresStore) -> None:
        assert await store.get_decision(uuid4()) is None

    async def test_get_many_returns_matching_subset(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        results = await store.get_many([narrative.backoff, narrative.batching, uuid4()])
        assert {d.decision_id for d in results} == {narrative.backoff, narrative.batching}

    async def test_get_many_empty_ids_returns_empty(self, store: PostgresStore) -> None:
        assert await store.get_many([]) == []


class TestGetManyCompact:
    """`get_many`'s compact projection (docs/components/04's "Compact
    projections are SQL-level" contract point) -- `precedent()`'s hydration
    step needs this rather than `get_many` + Python-side trimming."""

    async def test_truncates_outcome_in_sql(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        results = await store.get_many_compact([narrative.batching], compact_chars=5)
        assert len(results) == 1
        assert results[0].id == narrative.batching
        # narrative's batching decision outcome: "batch ingestion; retries unnecessary"
        assert results[0].outcome_truncated == "batch"

    async def test_returns_matching_subset_with_subject_refs(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        results = await store.get_many_compact(
            [narrative.backoff, narrative.batching, uuid4()], compact_chars=200
        )
        assert {c.id for c in results} == {narrative.backoff, narrative.batching}
        batching = next(c for c in results if c.id == narrative.batching)
        assert any(r.identifier == "portolan-ingest" for r in batching.subject_refs)

    async def test_empty_ids_returns_empty(self, store: PostgresStore) -> None:
        assert await store.get_many_compact([], compact_chars=200) == []


class TestRelevant:
    """FR-6.1 relevance matrix: scoped/unscoped x status x as_of x archived."""

    async def test_scoped_subject_matches_scoped_and_unscoped_decisions(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        results = await store.relevant(subject=("component", "portolan-ingest"))
        ids = {d.id for d in results}
        assert narrative.batching in ids  # scoped, matches subject
        assert narrative.general in ids  # unscoped, applies generally (FR-6.1)
        assert narrative.other_component not in ids  # scoped to a different subject
        assert narrative.backoff not in ids  # superseded, excluded by default status

    async def test_no_subject_filter_returns_regardless_of_scope(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        results = await store.relevant()
        ids = {d.id for d in results}
        assert narrative.batching in ids
        assert narrative.other_component in ids

    async def test_default_status_excludes_superseded_discarded_archived(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        ids = {d.id for d in await store.relevant()}
        assert narrative.backoff not in ids
        assert narrative.discarded not in ids
        assert narrative.archived not in ids

    async def test_explicit_status_filter(self, store: PostgresStore, narrative: Narrative) -> None:
        results = await store.relevant(status=["superseded"])
        assert {d.id for d in results} == {narrative.backoff}

    async def test_include_archived_expands_the_default_status_set(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        without = {d.id for d in await store.relevant()}
        with_archived = {d.id for d in await store.relevant(include_archived=True)}
        assert narrative.archived not in without
        assert narrative.archived in with_archived

    async def test_as_of_default_excludes_expired_valid_until(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        assert narrative.expired not in {d.id for d in await store.relevant()}

    async def test_as_of_in_the_past_includes_a_then_still_valid_decision(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        as_of = narrative.now - timedelta(days=3)  # within expired's [valid_from, valid_until)
        results = await store.relevant(as_of=as_of)
        assert narrative.expired in {d.id for d in results}

    async def test_domain_filter(self, store: PostgresStore, narrative: Narrative) -> None:
        assert await store.relevant(domains=["product"]) == []

    async def test_text_filter_matches_scenario_outcome_or_reasoning(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        results = await store.relevant(text="circuit breaker")
        assert {d.id for d in results} == {narrative.other_component}

    async def test_text_filter_escapes_ilike_metacharacters(self, store: PostgresStore) -> None:
        """`_` and `%` are ILIKE wildcards (any-one-char, any-run-of-chars); a
        literal search for either must not mis-match text that merely fits the
        wildcard pattern."""
        literal_underscore = _decision(outcome="reindex the user_id column")
        underscore_wildcard_trap = _decision(outcome="reindex the userXid column")
        literal_percent = _decision(outcome="roll out to 50% of users")
        percent_wildcard_trap = _decision(outcome="roll out to 50XXXXX of users")
        async with store.transaction() as tx:
            for d in (
                literal_underscore,
                underscore_wildcard_trap,
                literal_percent,
                percent_wildcard_trap,
            ):
                await store.insert_decision(tx, d, f"h-{d.decision_id}")
                await store.apply_transition(
                    tx, d.decision_id, "recorded", HUMAN.as_str(), None, None, "current"
                )

        underscore_results = {d.id for d in await store.relevant(text="user_id")}
        assert underscore_results == {literal_underscore.decision_id}

        percent_results = {d.id for d in await store.relevant(text="50% of users")}
        assert percent_results == {literal_percent.decision_id}

    async def test_compact_projection_truncates_outcome_in_sql(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        # Also matches the unscoped `general`/`supplement` decisions (FR-6.1); pick
        # out `other_component` specifically to check its truncated outcome.
        results = await store.relevant(subject=("component", "other-service"), compact_chars=5)
        other = next(d for d in results if d.id == narrative.other_component)
        assert isinstance(other, CompactDecision)
        assert other.outcome_truncated == "use a"

    async def test_full_projection_returns_hydrated_decisions(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        results = await store.relevant(subject=("component", "other-service"), compact_chars=None)
        other = next(d for d in results if d.decision_id == narrative.other_component)
        assert isinstance(other, Decision)
        assert other.refs

    async def test_deterministic_ordering_recency_then_id(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        results = await store.relevant(
            domains=["architecture"], status=["current"], compact_chars=None
        )
        recorded_ats = [d.recorded_at for d in results]
        assert recorded_ats == sorted(recorded_ats, reverse=True)

    async def test_limit_caps_result_count(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        results = await store.relevant(domains=["architecture"], status=["current"], limit=1)
        assert len(results) == 1


class TestHistory:
    async def test_predecessor_and_successor_chains(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        h = await store.history(narrative.batching)
        assert h.decision.decision_id == narrative.batching
        assert [d.decision_id for d in h.predecessors] == [narrative.backoff]

        h_backoff = await store.history(narrative.backoff)
        assert [d.decision_id for d in h_backoff.successors] == [narrative.batching]

    async def test_supplements_surfaced(self, store: PostgresStore, narrative: Narrative) -> None:
        h = await store.history(narrative.batching)
        assert [d.decision_id for d in h.supplements] == [narrative.supplement]

    async def test_includes_archived_and_discarded_targets(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        assert (await store.history(narrative.archived)).decision.status == "archived"
        assert (await store.history(narrative.discarded)).decision.status == "discarded"

    async def test_transitions_in_chronological_order(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        h = await store.history(narrative.backoff)
        ats = [t.at for t in h.transitions]
        assert ats == sorted(ats)
        assert [t.action for t in h.transitions] == ["recorded", "superseded"]

    async def test_links_include_both_directions(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        h = await store.history(narrative.batching)
        kinds_and_to_ids = {(link.kind, link.to_id) for link in h.links}
        assert ("SUPERSEDES", narrative.backoff) in kinds_and_to_ids

    async def test_unknown_decision_raises_decision_not_found(self, store: PostgresStore) -> None:
        with pytest.raises(DecisionNotFound):
            await store.history(uuid4())

    async def test_cyclic_supersedes_terminates_without_hanging(self, store: PostgresStore) -> None:
        """The Lifecycle Engine (not built yet) is what's supposed to keep SUPERSEDES
        acyclic; this is the read path's own defense-in-depth against a cycle that
        somehow lands in the data anyway (bare insert_link has no such check)."""
        a, b = _decision(), _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, a, "h-a")
            await store.insert_decision(tx, b, "h-b")
            await store.insert_link(tx, a.decision_id, b.decision_id, "SUPERSEDES")
            await store.insert_link(tx, b.decision_id, a.decision_id, "SUPERSEDES")

        h = await asyncio.wait_for(store.history(a.decision_id), timeout=5)
        assert h.decision.decision_id == a.decision_id
        assert b.decision_id in [d.decision_id for d in h.predecessors]
        assert b.decision_id in [d.decision_id for d in h.successors]


class TestChanges:
    """FR-6.5 changes feed: window / action / actor, paired with a compact projection."""

    async def test_since_window_excludes_earlier_transitions(self, store: PostgresStore) -> None:
        d1 = _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d1, "h1")
            await store.apply_transition(
                tx, d1.decision_id, "recorded", AGENT.as_str(), None, None, "current"
            )
        checkpoint = (await store.changes(actions=["recorded"]))[0][0].at

        d2 = _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d2, "h2")
            await store.apply_transition(
                tx, d2.decision_id, "recorded", AGENT.as_str(), None, None, "current"
            )

        results = await store.changes(since=checkpoint + timedelta(microseconds=1))
        ids = {compact.id for _, compact in results}
        assert d2.decision_id in ids
        assert d1.decision_id not in ids

    async def test_filter_by_action(self, store: PostgresStore, narrative: Narrative) -> None:
        results = await store.changes(actions=["archived"])
        assert {compact.id for _, compact in results} == {narrative.archived}
        assert all(t.action == "archived" for t, _ in results)

    async def test_filter_by_actor(self, store: PostgresStore, narrative: Narrative) -> None:
        results = await store.changes(actor=HUMAN)
        assert results
        assert all(t.actor == HUMAN for t, _ in results)

    async def test_paired_with_compact_projection_of_the_decision(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        results = await store.changes(actions=["superseded"])
        assert len(results) == 1
        transition, compact = results[0]
        assert transition.decision_id == narrative.backoff
        assert compact.id == narrative.backoff
        assert compact.status == "superseded"


class TestOpenQueue:
    async def test_shakiest_orders_item_confidence_then_decision_confidence_then_default_last(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        async with store.transaction() as tx:
            item_explicit = await store.enqueue(
                tx, "link", narrative.general, narrative.other_component, ENGINE, "r", 0.3
            )
            # backoff carries confidence=0.8 on the decision row itself.
            item_falls_back_to_decision = await store.enqueue(
                tx, "promote", narrative.backoff, None, AGENT, "r", None
            )
            # other_component has no confidence at all -> fallback default 1.0, sorted last.
            item_default_last = await store.enqueue(
                tx, "promote", narrative.other_component, None, HUMAN, "r", None
            )
        assert item_explicit is not None
        assert item_falls_back_to_decision is not None
        assert item_default_last is not None

        views = await store.open_queue(order="shakiest")
        order = [v.item.item_id for v in views]
        assert order.index(item_explicit) < order.index(item_falls_back_to_decision)
        assert order.index(item_falls_back_to_decision) < order.index(item_default_last)

        fallback_view = next(v for v in views if v.item.item_id == item_falls_back_to_decision)
        assert fallback_view.decision_confidence == 0.8

    async def test_oldest_orders_by_proposed_at(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        async with store.transaction() as tx:
            first = await store.enqueue(tx, "promote", narrative.backoff, None, AGENT, "r", 0.5)
        async with store.transaction() as tx:
            second = await store.enqueue(
                tx, "promote", narrative.other_component, None, HUMAN, "r", 0.5
            )
        views = await store.open_queue(order="oldest")
        assert [v.item.item_id for v in views] == [first, second]

    async def test_kinds_filter(self, store: PostgresStore, narrative: Narrative) -> None:
        async with store.transaction() as tx:
            promote_item = await store.enqueue(
                tx, "promote", narrative.backoff, None, AGENT, "r", 0.5
            )
            await store.enqueue(
                tx, "link", narrative.general, narrative.other_component, ENGINE, "r", 0.5
            )
        views = await store.open_queue(kinds=["promote"])
        assert [v.item.item_id for v in views] == [promote_item]


class TestBySource:
    async def test_filters_by_source(self, store: PostgresStore, narrative: Narrative) -> None:
        results = await store.by_source("portolan")
        assert {d.id for d in results} == {narrative.general, narrative.other_component}

    async def test_status_filter_narrows_the_result(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        results = await store.by_source("portolan", status=["not_promoted"])
        assert results == []

    async def test_unknown_filter_kwarg_raises_type_error(self, store: PostgresStore) -> None:
        with pytest.raises(TypeError):
            await store.by_source("meridian", bogus=True)


class TestKnn:
    async def test_returns_score_order_and_excludes_archived(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        query = _vector(0, 1.0)
        near = _vector(0, 0.9, 1, 0.1)
        far = _vector(1, 1.0)
        async with store.transaction() as tx:
            await store.upsert_embedding(tx, narrative.batching, query)
            await store.upsert_embedding(tx, narrative.other_component, near)
            await store.upsert_embedding(tx, narrative.general, far)
            await store.upsert_embedding(
                tx, narrative.archived, query
            )  # perfect match, but archived

        results = await store.knn(query, k=3)
        ids = [r[0] for r in results]
        assert narrative.archived not in ids
        assert ids == [narrative.batching, narrative.other_component, narrative.general]
        assert results[0][1] > results[1][1] > results[2][1]

    async def test_exclude_ids(self, store: PostgresStore, narrative: Narrative) -> None:
        query = _vector(0, 1.0)
        async with store.transaction() as tx:
            await store.upsert_embedding(tx, narrative.batching, query)
            await store.upsert_embedding(tx, narrative.other_component, query)
        results = await store.knn(query, k=5, exclude_ids=[narrative.batching])
        assert narrative.batching not in [r[0] for r in results]


class TestUnembedded:
    async def test_returns_decisions_without_an_embeddings_row(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        async with store.transaction() as tx:
            await store.upsert_embedding(tx, narrative.backoff, [0.0] * 768)
        ids = {d.decision_id for d in await store.unembedded(limit=100)}
        assert narrative.backoff not in ids
        assert narrative.batching in ids

    async def test_limit_caps_result_count(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        assert len(await store.unembedded(limit=1)) == 1


class TestUndiscovered:
    async def test_returns_ids_pending_discovery(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        async with store.transaction() as tx:
            await store.upsert_embedding(tx, narrative.backoff, [0.0] * 768)
            await store.upsert_embedding(tx, narrative.batching, [0.0] * 768)
            await store.mark_discovered(tx, [narrative.backoff])
        results = await store.undiscovered(limit=100)
        assert narrative.batching in results
        assert narrative.backoff not in results


class TestAgingUnrecommended:
    async def test_excludes_decisions_with_an_open_promote_item(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        async with store.transaction() as tx:
            await store.enqueue(tx, "promote", narrative.other_component, None, AGENT, "ready", 0.7)
        ids = {d.id for d in await store.aging_unrecommended(older_than=narrative.now, limit=100)}
        assert narrative.other_component not in ids
        assert narrative.general in ids

    async def test_only_current_short_term_decisions(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        ids = {d.id for d in await store.aging_unrecommended(older_than=narrative.now, limit=100)}
        assert narrative.backoff not in ids  # superseded
        assert narrative.discarded not in ids
        assert narrative.archived not in ids


class TestArchivalEligible:
    """FR-3.4: the auto-archival candidate set, blocked by any open queue item."""

    async def test_open_queue_item_blocks_eligibility_on_either_side(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        async with store.transaction() as tx:
            await store.enqueue(
                tx, "supersede", narrative.other_component, narrative.general, AGENT, "r", 0.6
            )
        cutoff = narrative.now + timedelta(days=1)
        results = await store.archival_eligible(cutoff=cutoff)
        assert narrative.other_component not in results  # blocked as the item's decision_id
        assert narrative.general not in results  # blocked as the item's target_id

    async def test_eligible_current_decision_with_no_open_items(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        cutoff = narrative.now + timedelta(days=1)
        assert narrative.general in await store.archival_eligible(cutoff=cutoff)

    async def test_cutoff_excludes_recent_decisions(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        cutoff = narrative.now - timedelta(days=100)
        assert await store.archival_eligible(cutoff=cutoff) == []

    async def test_long_term_decisions_are_never_eligible(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        lt = _decision(tier="long_term", recorded_at=narrative.now - timedelta(days=200))
        async with store.transaction() as tx:
            await store.insert_decision(tx, lt, "h-lt")
            await store.apply_transition(
                tx, lt.decision_id, "recorded", HUMAN.as_str(), None, None, "current"
            )
        cutoff = narrative.now + timedelta(days=1)
        assert lt.decision_id not in await store.archival_eligible(cutoff=cutoff)


class TestExportRows:
    async def test_bundle_includes_domains_registry_and_has_no_embeddings_field(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        async with store.transaction() as tx:
            await store.upsert_embedding(tx, narrative.backoff, [0.0] * 768)
        bundle = await store.export_rows()
        assert {d.name for d in bundle.domains} == {"architecture", "product"}
        assert not hasattr(bundle, "embeddings")
        assert narrative.backoff in {d.decision_id for d in bundle.decisions}

    async def test_filters_apply_to_decisions_and_their_related_rows(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        bundle = await store.export_rows(domains=["architecture"], status=["superseded"])
        assert {d.decision_id for d in bundle.decisions} == {narrative.backoff}
        assert any(t.decision_id == narrative.backoff for t in bundle.transitions)
        assert any(link.to_id == narrative.backoff for link in bundle.links)

    async def test_schema_version_is_stamped(
        self, store: PostgresStore, narrative: Narrative
    ) -> None:
        assert (await store.export_rows()).schema_version >= 1
