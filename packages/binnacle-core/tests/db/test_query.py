"""Integration tests for `precedent()` (docs/binnacle-core/components/04-query-and-assist.md
"precedent()"; REQUIREMENTS FR-6.3; needs a live Postgres, see conftest.pg_dsn).

Exercises `application.query.precedent` directly against `PostgresStore` +
`StubEmbedder`, per the task brief: "insert embeddings directly via
store.upsert_embedding with hand-chosen vectors". `StubEmbedder` is
deterministic but hash-derived, so the *question's own* vector cannot be
chosen outright -- instead each fixture perturbs that computed vector by
flipping the sign of a known, strictly-increasing subset of its components.
For a vector q and a flip set F, cos_sim(perturb(q, F), q) = 1 - 2 *
sum_{i in F}(q[i]**2) / |q|**2 -- so nesting flip sets (flips=1 subset of
flips=3 subset of ...) gives a *provably* monotonic decreasing similarity
ladder, and a full flip (every component) gives cos_sim = -1.0 exactly. No
real embedding semantics or numeric library needed; only float non-zero-ness,
which hash-derived components satisfy with overwhelming probability.

`TestClientWiring` adds one thin end-to-end check that `Binnacle.precedent()`
actually delegates to this module and applies `config.compact_outcome_chars`
-- `application.query.precedent` itself carries the real coverage.

`TestRelevantSorting` (Task 4) covers `relevant()`'s `sort`/`order` params
directly against `PostgresStore`, plus one `Binnacle`-level test for
`last_touched_at` (needs `supplement()`, a lifecycle verb the store itself
doesn't expose).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from binnacle_core.adapters.postgres_store import PostgresStore
from binnacle_core.application.config import BinnacleConfig
from binnacle_core.application.query import precedent
from binnacle_core.client import Binnacle
from binnacle_core.domain.models import Actor, Decision, NewDecision
from tests.helpers import StubEmbedder

HUMAN = Actor(kind="human", id="alice")
AGENT = Actor(kind="agent", id="meridian/s1")
ENGINE = Actor(kind="engine", id="binnacle")

QUESTION = "how should retry backoff be configured for ingestion?"
DIM = 32


def _decision(**overrides: Any) -> Decision:
    base: dict[str, Any] = {
        "decision_id": uuid4(),
        "domain": "eng",
        "tier": "short_term",
        "status": "current",
        "scenario": "how should transient ingestion failures be handled?",
        "outcome": "retry with exponential backoff",
        "reasoning": "avoids thundering herd on recovery",
        "source": "test-suite",
        "recorded_by": AGENT,
        "recorded_at": datetime.now(UTC),
    }
    base.update(overrides)
    return Decision(**base)


def _perturb(vector: list[float], flips: int) -> list[float]:
    """Flip the sign of `vector[:flips]`. See module docstring: nested flip
    sets give a strictly monotonic cosine-similarity ladder relative to
    `vector` itself, with `flips=0` an exact match (similarity 1.0) and
    `flips=len(vector)` its exact opposite (similarity -1.0)."""
    v = list(vector)
    for i in range(flips):
        v[i] = -v[i]
    return v


@pytest.fixture()
async def store(pg_dsn: str, scratch_schema: str) -> AsyncIterator[PostgresStore]:
    s = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=DIM)
    await s.migrate()
    async with s.transaction() as tx:
        await s.upsert_domain(
            tx, "eng", "engineering decisions", True, HUMAN.as_str(), "domain_created", None
        )
        await s.upsert_domain(
            tx, "product", "product decisions", True, HUMAN.as_str(), "domain_created", None
        )
    yield s
    await s.aclose()


@pytest.fixture()
def embedder() -> StubEmbedder:
    return StubEmbedder(dim=DIM)


@pytest.fixture()
async def query_vector(embedder: StubEmbedder) -> list[float]:
    [v] = await embedder.embed([QUESTION])
    return v


async def _seed(store: PostgresStore, decision: Decision, vector: list[float]) -> UUID:
    async with store.transaction() as tx:
        await store.insert_decision(tx, decision, f"h-{decision.decision_id}")
        await store.apply_transition(
            tx,
            decision.decision_id,
            "recorded",
            decision.recorded_by.as_str(),
            None,
            None,
            decision.status,
        )
        await store.upsert_embedding(tx, decision.decision_id, vector)
    return decision.decision_id


async def _mark_superseded(store: PostgresStore, decision_id: UUID) -> None:
    async with store.transaction() as tx:
        await store.apply_transition(
            tx, decision_id, "superseded", ENGINE.as_str(), None, None, "superseded"
        )


async def _mark_not_promoted(store: PostgresStore, decision_id: UUID) -> None:
    async with store.transaction() as tx:
        await store.apply_transition(
            tx, decision_id, "declined", HUMAN.as_str(), "not ready", None, "not_promoted"
        )


async def _mark_archived(store: PostgresStore, decision_id: UUID) -> None:
    async with store.transaction() as tx:
        await store.apply_transition(
            tx, decision_id, "archived", ENGINE.as_str(), None, None, "archived"
        )


async def _mark_discarded(store: PostgresStore, decision_id: UUID) -> None:
    async with store.transaction() as tx:
        await store.apply_transition(
            tx, decision_id, "discarded", AGENT.as_str(), "malformed", None, "discarded"
        )


class TestScoreOrder:
    """FR-6.3: results come back similarity-descending, ties broken by id."""

    async def test_orders_by_similarity_descending(
        self, store: PostgresStore, embedder: StubEmbedder, query_vector: list[float]
    ) -> None:
        near = await _seed(store, _decision(), _perturb(query_vector, 0))
        mid = await _seed(store, _decision(), _perturb(query_vector, 1))
        far = await _seed(store, _decision(), _perturb(query_vector, 5))

        hits = await precedent(store, embedder, QUESTION, limit=10)

        assert [h.decision.id for h in hits] == [near, mid, far]
        assert hits[0].similarity > hits[1].similarity > hits[2].similarity

    async def test_ties_break_by_decision_id(
        self, store: PostgresStore, embedder: StubEmbedder, query_vector: list[float]
    ) -> None:
        d1 = await _seed(store, _decision(), query_vector)
        d2 = await _seed(store, _decision(), query_vector)

        hits = await precedent(store, embedder, QUESTION, limit=10)

        tied_ids = [h.decision.id for h in hits if h.decision.id in (d1, d2)]
        assert tied_ids == sorted(tied_ids, key=str)


class TestDeadHistory:
    """FR-6.3: superseded/not_promoted are history, included and labeled by
    default -- `include_dead=False` is what drops them, not their mere
    existence."""

    async def test_include_dead_default_returns_superseded_and_not_promoted(
        self, store: PostgresStore, embedder: StubEmbedder, query_vector: list[float]
    ) -> None:
        current = await _seed(store, _decision(), _perturb(query_vector, 0))
        superseded = await _seed(store, _decision(), _perturb(query_vector, 1))
        await _mark_superseded(store, superseded)
        not_promoted = await _seed(store, _decision(), _perturb(query_vector, 2))
        await _mark_not_promoted(store, not_promoted)

        hits = await precedent(store, embedder, QUESTION, limit=10)
        by_id = {h.decision.id: h for h in hits}

        assert {current, superseded, not_promoted} <= set(by_id)
        assert by_id[superseded].decision.status == "superseded"
        assert by_id[not_promoted].decision.status == "not_promoted"

    async def test_include_dead_false_drops_superseded_and_not_promoted(
        self, store: PostgresStore, embedder: StubEmbedder, query_vector: list[float]
    ) -> None:
        current = await _seed(store, _decision(), _perturb(query_vector, 0))
        superseded = await _seed(store, _decision(), _perturb(query_vector, 1))
        await _mark_superseded(store, superseded)
        not_promoted = await _seed(store, _decision(), _perturb(query_vector, 2))
        await _mark_not_promoted(store, not_promoted)

        hits = await precedent(store, embedder, QUESTION, limit=10, include_dead=False)
        ids = {h.decision.id for h in hits}

        assert ids == {current}


class TestArchivedDiscardedNeverReturned:
    """Archived/discarded are gone, not history -- excluded unconditionally,
    `include_dead` notwithstanding (store.knn's join, exercised end-to-end
    here through precedent())."""

    async def test_archived_and_discarded_excluded_even_with_perfect_similarity(
        self, store: PostgresStore, embedder: StubEmbedder, query_vector: list[float]
    ) -> None:
        current = await _seed(store, _decision(), _perturb(query_vector, 0))
        archived = await _seed(store, _decision(), query_vector)  # perfect match
        await _mark_archived(store, archived)
        discarded = await _seed(store, _decision(), query_vector)  # perfect match
        await _mark_discarded(store, discarded)

        hits = await precedent(store, embedder, QUESTION, limit=10, include_dead=True)
        ids = {h.decision.id for h in hits}

        assert current in ids
        assert archived not in ids
        assert discarded not in ids


class TestAttributeFilters:
    async def test_domains_filter_narrows_result(
        self, store: PostgresStore, embedder: StubEmbedder, query_vector: list[float]
    ) -> None:
        eng = await _seed(store, _decision(domain="eng"), _perturb(query_vector, 0))
        product = await _seed(store, _decision(domain="product"), _perturb(query_vector, 1))

        hits = await precedent(store, embedder, QUESTION, domains=["eng"], limit=10)
        ids = {h.decision.id for h in hits}

        assert eng in ids
        assert product not in ids

    async def test_tiers_filter_narrows_result(
        self, store: PostgresStore, embedder: StubEmbedder, query_vector: list[float]
    ) -> None:
        short = await _seed(store, _decision(tier="short_term"), _perturb(query_vector, 0))
        long_ = await _seed(
            store, _decision(tier="long_term", status="current"), _perturb(query_vector, 1)
        )

        hits = await precedent(store, embedder, QUESTION, tiers=["long_term"], limit=10)
        ids = {h.decision.id for h in hits}

        assert long_ in ids
        assert short not in ids


class TestLimitHonoredWhenFiltersDropRows:
    """The over-fetch case: `store.knn` doesn't know about domains/tiers, so
    the raw k-NN ranking can be dominated by candidates the filter is about to
    drop. `precedent()` must request more than `limit` from `knn` so the
    filtered-in candidates aren't starved -- a naive `knn(vector, limit)`
    call would see only the (filtered-out) product decisions here and return
    zero results after filtering to domains=["eng"]."""

    async def test_over_fetches_so_filtered_candidates_survive(
        self, store: PostgresStore, embedder: StubEmbedder, query_vector: list[float]
    ) -> None:
        # 5 product decisions, all a perfect match -- rank strictly above every
        # eng decision below in raw (pre-filter) similarity.
        for _ in range(5):
            await _seed(store, _decision(domain="product"), query_vector)

        # 3 eng decisions, each less-than-perfect but still ordered among
        # themselves (fewer flips = higher similarity).
        eng1 = await _seed(store, _decision(domain="eng"), _perturb(query_vector, 1))
        eng2 = await _seed(store, _decision(domain="eng"), _perturb(query_vector, 2))
        eng3 = await _seed(store, _decision(domain="eng"), _perturb(query_vector, 3))

        hits = await precedent(store, embedder, QUESTION, domains=["eng"], limit=3)

        assert [h.decision.id for h in hits] == [eng1, eng2, eng3]


class TestAdaptiveOverfetch:
    """FR-6.3 fix: a single `k = limit * 4` round can under-fill when a
    filter rejects most of that round's candidates, even though the index
    holds enough matches overall. `precedent()` escalates `k` (see
    `application.query._OVERFETCH_FACTOR`/`_MAX_OVERFETCH_ROUNDS`/
    `_OVERFETCH_CAP`) instead of accepting the short result."""

    async def test_escalates_when_first_round_under_fills_but_index_has_more(
        self, store: PostgresStore, embedder: StubEmbedder, query_vector: list[float]
    ) -> None:
        # 15 product decisions, all a perfect match -- round 1's k=limit*4=12
        # nearest neighbors are entirely these (distance 0 beats every eng
        # decision below), so a single, non-escalating round would filter
        # domains=["eng"] down to zero even though 3 eng decisions exist.
        for _ in range(15):
            await _seed(store, _decision(domain="product"), query_vector)

        eng1 = await _seed(store, _decision(domain="eng"), _perturb(query_vector, 1))
        eng2 = await _seed(store, _decision(domain="eng"), _perturb(query_vector, 2))
        eng3 = await _seed(store, _decision(domain="eng"), _perturb(query_vector, 3))

        hits = await precedent(store, embedder, QUESTION, domains=["eng"], limit=3)

        assert [h.decision.id for h in hits] == [eng1, eng2, eng3]

    async def test_returns_all_matches_when_index_smaller_than_limit(
        self, store: PostgresStore, embedder: StubEmbedder, query_vector: list[float]
    ) -> None:
        # Only 2 decisions total exist, well under `limit` -- escalation must
        # recognize the index is exhausted (round 1 returns fewer rows than
        # requested) and stop after one round instead of looping.
        first = await _seed(store, _decision(), _perturb(query_vector, 0))
        second = await _seed(store, _decision(), _perturb(query_vector, 1))

        hits = await precedent(store, embedder, QUESTION, limit=10)

        assert {h.decision.id for h in hits} == {first, second}


class TestClientWiring:
    """Thin end-to-end check that `Binnacle.precedent()` delegates to
    `application.query.precedent` and applies `config.compact_outcome_chars`
    -- the real coverage lives in the classes above."""

    async def test_precedent_delegates_and_truncates_per_config(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        config = BinnacleConfig(
            dsn=pg_dsn,
            schema_name=scratch_schema,
            embedder=StubEmbedder(dim=DIM),
            embedding_dim=DIM,
            compact_outcome_chars=5,
        )
        client = Binnacle(config)
        await client.migrate()
        await client.add_domain("eng", "engineering", actor=HUMAN)

        raw_store = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=DIM)
        try:
            [vector] = await config.embedder.embed([QUESTION])
            decision = _decision(outcome="a much longer outcome than five characters")
            await _seed(raw_store, decision, vector)

            hits = await client.precedent(QUESTION, limit=5)

            assert len(hits) == 1
            assert hits[0].decision.id == decision.decision_id
            assert hits[0].decision.outcome_truncated == "a muc"
            assert hits[0].similarity == pytest.approx(1.0)
        finally:
            await raw_store.aclose()
            await client.aclose()


class TestRelevantSorting:
    """Task 4: `relevant()`'s ordering is now a parameter -- four closed sort
    keys, two directions. Defaults reproduce the pre-existing `recorded_at
    DESC` behaviour exactly; `last_touched_at` is derived from
    `MAX(transitions.at)` rather than the immutable `recorded_at` column, so
    a decision recorded long ago but supplemented recently stops ranking as
    stalest."""

    async def test_defaults_preserve_recorded_at_desc(self, store: PostgresStore) -> None:
        """The pre-existing ordering is the default; this addition
        parameterizes it rather than changing it."""
        base = datetime(2024, 1, 1, tzinfo=UTC)
        ids = [
            await _seed(store, _decision(recorded_at=base + timedelta(days=i)), [1.0] * DIM)
            for i in range(3)
        ]

        default = await store.relevant(limit=50)
        explicit = await store.relevant(sort="recorded_at", order="desc", limit=50)

        assert (
            [d.id for d in default.items] == [d.id for d in explicit.items] == list(reversed(ids))
        )

    async def test_oldest_first_reverses_the_default(self, store: PostgresStore) -> None:
        base = datetime(2024, 1, 1, tzinfo=UTC)
        for i in range(3):
            await _seed(store, _decision(recorded_at=base + timedelta(days=i)), [1.0] * DIM)

        newest = await store.relevant(sort="recorded_at", order="desc", limit=50)
        oldest = await store.relevant(sort="recorded_at", order="asc", limit=50)

        assert [d.id for d in oldest.items] == list(reversed([d.id for d in newest.items]))

    async def test_last_touched_at_ranks_a_supplemented_decision_as_recent(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        """The whole point of the derived key: a decision recorded long ago but
        supplemented recently is NOT stale, and recorded_at would rank it
        stalest. supplement() writes a transition on both sides -- this needs
        `Binnacle`, since `supplement()` is a lifecycle verb the store itself
        doesn't expose."""
        config = BinnacleConfig(
            dsn=pg_dsn,
            schema_name=scratch_schema,
            embedder=StubEmbedder(dim=DIM),
            embedding_dim=DIM,
        )
        client = Binnacle(config)
        await client.migrate()
        await client.add_domain("eng", "engineering", actor=HUMAN)

        raw_store = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=DIM)
        try:
            old = datetime(2021, 1, 1, tzinfo=UTC)
            target = await _seed(raw_store, _decision(recorded_at=old), [1.0] * DIM)
            await _seed(raw_store, _decision(recorded_at=old + timedelta(days=1)), [1.0] * DIM)

            oldest_by_record = await client.relevant(sort="recorded_at", order="asc", limit=1)
            target_id = oldest_by_record.items[0].id
            assert target_id == target

            newer = await client.record(
                NewDecision(
                    domain="eng",
                    scenario="a later decision",
                    outcome="use the newer approach",
                    reasoning="keeps the system current",
                    source="test-suite",
                ),
                actor=HUMAN,
            )
            await client.supplement(newer.decision_id, target_id, actor=HUMAN)

            by_touch = await client.relevant(sort="last_touched_at", order="asc", limit=50)
            assert by_touch.items[0].id != target_id, "supplementing should stop ranking it stalest"
        finally:
            await raw_store.aclose()
            await client.aclose()


class TestChangesTiebreaker:
    """Task 7: `changes()` gains `after_id` tiebreaker to handle transitions
    sharing the same timestamp. Without it, they reappear on the next
    `since=`-based fetch.

    Postgres's `now()` returns *transaction start time*, so two decisions
    recorded inside one `store.transaction()` block get an identical `at` --
    the exact condition the tiebreaker exists for. That lets the test force a
    genuine tie instead of relying on a plain seeding loop, where
    monotonically increasing `transition_id`s and `at`s never collide."""

    async def test_after_id_excludes_seen_row_but_keeps_its_timestamp_tied_sibling(
        self, store: PostgresStore
    ) -> None:
        older_id, newer_id = uuid4(), uuid4()
        async with store.transaction() as tx:
            await store.insert_decision(tx, _decision(decision_id=older_id), f"h-{older_id}")
            await store.apply_transition(
                tx, older_id, "recorded", HUMAN.as_str(), None, None, "current"
            )
            await store.insert_decision(tx, _decision(decision_id=newer_id), f"h-{newer_id}")
            await store.apply_transition(
                tx, newer_id, "recorded", HUMAN.as_str(), None, None, "current"
            )

        # ORDER BY t.at DESC, t.transition_id DESC: with `at` tied, the
        # higher transition_id (newer_id's, inserted second) sorts first.
        seen, unseen = await store.changes(limit=2)
        seen_transition, _ = seen
        unseen_transition, _ = unseen
        assert seen_transition.decision_id == newer_id
        assert unseen_transition.decision_id == older_id
        assert seen_transition.at == unseen_transition.at, (
            "test invalid unless the two transitions share an `at`"
        )

        following = await store.changes(
            since=seen_transition.at, after_id=seen_transition.transition_id
        )
        following_ids = {t.transition_id for t, _ in following}

        assert seen_transition.transition_id not in following_ids
        assert unseen_transition.transition_id in following_ids, (
            "the timestamp-tied sibling must still come back -- `since` alone "
            "can't distinguish it from the already-seen row, only after_id can"
        )
