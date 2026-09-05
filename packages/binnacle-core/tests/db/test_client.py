"""Integration tests for the `Binnacle` public client (needs a live Postgres;
see conftest.pg_dsn). docs/binnacle-core/components/01-configuration-and-client.md's
"Acceptance" is the spec these tests mirror: an embedder-shaped test drives
record -> recommend -> promote_refined -> query -> export end-to-end through
the public API only, plus the two boundary-error cases named there.

This module exercises `Binnacle` itself (config wiring, actor passthrough,
registry verbs it owns outright) — it does NOT re-litigate Lifecycle Engine
state-machine coverage, already exhaustive in tests/db/test_lifecycle.py.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from binnacle_core.application.config import BinnacleConfig
from binnacle_core.client import Binnacle
from binnacle_core.domain.errors import AuthorityViolation, InactiveDomain, UnknownDomain
from binnacle_core.domain.models import Actor, NewDecision, OptionConsidered, Ref
from tests.helpers import StubEmbedder

HUMAN = Actor("human", "alice")
AGENT = Actor("agent", "meridian/sess-1")


@pytest.fixture()
async def bn(pg_dsn: str, scratch_schema: str) -> AsyncIterator[Binnacle]:
    config = BinnacleConfig(dsn=pg_dsn, schema_name=scratch_schema, embedder=StubEmbedder(dim=8))
    client = Binnacle(config)
    await client.migrate()
    await client.add_domain("eng", "engineering", actor=HUMAN)
    yield client
    await client.aclose()


def _nd(**overrides: object) -> NewDecision:
    base: dict[str, object] = {
        "domain": "eng",
        "scenario": "adopt exponential backoff for retries",
        "outcome": "retries use exponential backoff with jitter",
        "reasoning": "reduces thundering herd under load",
        "source": "test-suite",
    }
    base.update(overrides)
    return NewDecision(**base)


class TestConstruction:
    async def test_construction_performs_no_io(self, pg_dsn: str) -> None:
        """`Binnacle(config)` must not touch the network — an unreachable dsn
        (bogus port) still constructs cleanly; only `migrate()`/verbs touch I/O."""
        config = BinnacleConfig(
            dsn="postgresql://nobody@nowhere:1/nope", embedder=StubEmbedder(dim=8)
        )
        client = Binnacle(config)
        assert client is not None

    async def test_migrate_then_aclose_roundtrip(self, pg_dsn: str, scratch_schema: str) -> None:
        config = BinnacleConfig(
            dsn=pg_dsn, schema_name=scratch_schema, embedder=StubEmbedder(dim=8)
        )
        client = Binnacle(config)
        await client.migrate()
        assert await client.domains() == []
        await client.aclose()


class TestNarrativeAcceptance:
    """docs/binnacle-core/components/01 "Acceptance": record (agent) -> recommend ->
    promote_refined (human, generalized subjects + amended outcome) ->
    relevant/compact -> history shows PROMOTED_FROM + refined payload ->
    export — driven entirely through the public `Binnacle` API."""

    async def test_record_recommend_promote_refined_query_export(self, bn: Binnacle) -> None:
        source = await bn.record(_nd(), actor=AGENT)
        assert source.tier == "short_term"
        assert source.status == "current"

        item_id = await bn.recommend(source.decision_id, actor=AGENT, reason="looks solid")
        assert item_id is not None

        refined = await bn.promote_refined(
            [source.decision_id],
            refined=_nd(
                scenario="standardize retry backoff across services",
                outcome="all services use exponential backoff with jitter (generalized)",
            ),
            actor=HUMAN,
        )
        assert refined.tier == "long_term"
        assert refined.status == "current"

        # relevant/compact: the long-term refined decision is visible, current,
        # and its outcome is truncated per config.compact_outcome_chars.
        compact = await bn.relevant(domains=["eng"], tier="long_term")
        assert any(d.id == refined.decision_id for d in compact.items)

        # history() on the SOURCE shows the PROMOTED_FROM link and the refined
        # transition payload (FR-4.6).
        history = await bn.history(source.decision_id)
        assert history.decision.status == "promoted"
        promoted_transitions = [t for t in history.transitions if t.action == "promoted"]
        assert len(promoted_transitions) == 1
        assert promoted_transitions[0].payload == {
            "refined": True,
            "target": str(refined.decision_id),
        }
        assert any(
            link.kind == "PROMOTED_FROM"
            and link.from_id == refined.decision_id
            and link.to_id == source.decision_id
            for link in history.links
        )

        # export: the refined long-term decision and its source both appear,
        # as a JSON-safe dict (FR-6.6) -- see TestExport for the full contract.
        document = await bn.export()
        exported_ids = {d["decision_id"] for d in document["decisions"]}
        assert {str(source.decision_id), str(refined.decision_id)} <= exported_ids
        assert any(dom["name"] == "eng" for dom in document["domains"])


class TestBoundaryErrors:
    async def test_promote_by_non_human_raises_authority_violation(self, bn: Binnacle) -> None:
        source = await bn.record(_nd(), actor=AGENT)
        item_id = await bn.recommend(source.decision_id, actor=AGENT, reason=None)
        assert item_id is not None
        with pytest.raises(AuthorityViolation):
            await bn.promote(item_id, actor=AGENT)

    async def test_record_unknown_domain_raises_unknown_domain(self, bn: Binnacle) -> None:
        with pytest.raises(UnknownDomain):
            await bn.record(_nd(domain="does-not-exist"), actor=AGENT)


class TestDomainRegistry:
    async def test_add_domain_is_human_only(self, bn: Binnacle) -> None:
        with pytest.raises(AuthorityViolation):
            await bn.add_domain("product", "product decisions", actor=AGENT)

    async def test_add_then_list_domains(self, bn: Binnacle) -> None:
        await bn.add_domain("product", "product decisions", actor=HUMAN)
        names = {d.name for d in await bn.domains()}
        assert {"eng", "product"} <= names

    async def test_update_domain_preserves_active_and_changes_description(
        self, bn: Binnacle
    ) -> None:
        await bn.update_domain("eng", "engineering (renamed)", actor=HUMAN)
        record = next(d for d in await bn.domains() if d.name == "eng")
        assert record.description == "engineering (renamed)"
        assert record.active is True

    async def test_update_unknown_domain_raises_unknown_domain(self, bn: Binnacle) -> None:
        with pytest.raises(UnknownDomain):
            await bn.update_domain("nope", "x", actor=HUMAN)

    async def test_deactivate_domain_preserves_description(self, bn: Binnacle) -> None:
        await bn.deactivate_domain("eng", actor=HUMAN, reason="reorg")
        record = next(d for d in await bn.domains() if d.name == "eng")
        assert record.active is False
        assert record.description == "engineering"

    async def test_deactivate_domain_is_human_only(self, bn: Binnacle) -> None:
        with pytest.raises(AuthorityViolation):
            await bn.deactivate_domain("eng", actor=AGENT)

    async def test_deactivate_unknown_domain_raises_unknown_domain(self, bn: Binnacle) -> None:
        with pytest.raises(UnknownDomain):
            await bn.deactivate_domain("nope", actor=HUMAN)

    async def test_record_into_deactivated_domain_raises_inactive_domain(
        self, bn: Binnacle
    ) -> None:
        """Ruling: `deactivate_domain` previously gated nothing -- recording
        into a deactivated domain silently succeeded. It must now be refused,
        typed, so callers can branch on it distinctly from `UnknownDomain`."""
        await bn.deactivate_domain("eng", actor=HUMAN, reason="reorg")
        with pytest.raises(InactiveDomain):
            await bn.record(_nd(), actor=AGENT)

    async def test_record_long_term_into_deactivated_domain_raises_inactive_domain(
        self, bn: Binnacle
    ) -> None:
        await bn.deactivate_domain("eng", actor=HUMAN, reason="reorg")
        with pytest.raises(InactiveDomain):
            await bn.record_long_term(_nd(), actor=HUMAN)

    async def test_promote_refined_into_deactivated_domain_raises_inactive_domain(
        self, bn: Binnacle
    ) -> None:
        """`promote_refined`'s `refined` decision goes through the same
        domain-registration check as any other recording (it shares
        `insert_new_decision`) -- a source recorded before deactivation can
        still exist, but the refined copy's domain must still be active."""
        source = await bn.record(_nd(), actor=AGENT)
        await bn.deactivate_domain("eng", actor=HUMAN, reason="reorg")
        with pytest.raises(InactiveDomain):
            await bn.promote_refined([source.decision_id], refined=_nd(), actor=HUMAN)

    async def test_readding_a_deactivated_domain_reactivates_it_for_recording(
        self, bn: Binnacle
    ) -> None:
        """`add_domain` re-registering an existing name is the documented
        reactivation path (docs/binnacle-core/components/01) -- no separate "reactivate"
        verb exists."""
        await bn.deactivate_domain("eng", actor=HUMAN, reason="reorg")
        with pytest.raises(InactiveDomain):
            await bn.record(_nd(), actor=AGENT)

        await bn.add_domain("eng", "engineering", actor=HUMAN)

        record = next(d for d in await bn.domains() if d.name == "eng")
        assert record.active is True
        decision = await bn.record(_nd(), actor=AGENT)
        assert decision.status == "current"


class TestQueryDelegation:
    async def test_relevant_compact_truncates_per_config(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        config = BinnacleConfig(
            dsn=pg_dsn,
            schema_name=scratch_schema,
            embedder=StubEmbedder(dim=8),
            compact_outcome_chars=5,
        )
        client = Binnacle(config)
        await client.migrate()
        await client.add_domain("eng", "engineering", actor=HUMAN)
        await client.record(_nd(outcome="a very long outcome string indeed"), actor=AGENT)
        compact = await client.relevant(domains=["eng"])
        assert compact.items
        assert all(len(d.outcome_truncated) <= 5 for d in compact.items)
        await client.aclose()

    async def test_relevant_full_projection_untruncated(self, bn: Binnacle) -> None:
        long_outcome = "x" * 500
        await bn.record(_nd(outcome=long_outcome), actor=AGENT)
        full = await bn.relevant(domains=["eng"], projection="full")
        assert any(d.outcome == long_outcome for d in full.items)

    async def test_queue_returns_open_recommendation(self, bn: Binnacle) -> None:
        source = await bn.record(_nd(), actor=AGENT)
        await bn.recommend(source.decision_id, actor=AGENT, reason="ready")
        open_items = await bn.queue(kinds=["promote"])
        assert any(v.item.decision_id == source.decision_id for v in open_items.items)

    async def test_get_many_and_by_source(self, bn: Binnacle) -> None:
        d1 = await bn.record(_nd(source="svc-a"), actor=AGENT)
        d2 = await bn.record(_nd(source="svc-a"), actor=AGENT)
        fetched = await bn.get_many([d1.decision_id, d2.decision_id])
        assert {d.decision_id for d in fetched} == {d1.decision_id, d2.decision_id}
        by_source = await bn.by_source("svc-a")
        assert {d.id for d in by_source} >= {d1.decision_id, d2.decision_id}

    async def test_changes_reflects_record_transition(self, bn: Binnacle) -> None:
        source = await bn.record(_nd(), actor=AGENT)
        changes = await bn.changes(actions=["recorded"])
        assert any(t.decision_id == source.decision_id for t, _ in changes)

    async def test_relevant_evidence_filter_is_forwarded(self, bn: Binnacle) -> None:
        """Store-level coverage (tests/db/test_query.py TestEvidenceFilter)
        proves `PostgresStore._relevant_where` builds the right SQL for
        `evidence`; the signature-parity test
        (tests/unit/test_query_signatures.py) only guards that
        `relevant()`/`relevant_count()` accept the same parameter *names*.
        Neither would catch `Binnacle.relevant()` mis-forwarding (or
        dropping) the `evidence` kwarg on its way to the store call
        underneath -- this exercises that wiring directly."""
        cited = await bn.record(
            _nd(refs=[Ref(role="evidence", kind="session", identifier="sess-42", note=None)]),
            actor=AGENT,
        )
        await bn.record(_nd(), actor=AGENT)  # cites nothing -- must not match

        page = await bn.relevant(evidence=("session", "sess-42"))

        assert [d.id for d in page.items] == [cited.decision_id]
        assert await bn.relevant_count(evidence=("session", "sess-42")) == 1

    async def test_relevant_expiring_before_filter_is_forwarded(self, bn: Binnacle) -> None:
        """Same rationale as `test_relevant_evidence_filter_is_forwarded`
        above, for `expiring_before` (tests/db/test_query.py
        TestExpiringBeforeFilter has the store-level SQL coverage)."""
        soon = datetime.now(UTC) + timedelta(days=7)
        far = datetime.now(UTC) + timedelta(days=30)
        expiring = await bn.record(_nd(valid_until=soon), actor=AGENT)
        await bn.record(_nd(valid_until=None), actor=AGENT)  # never expires
        await bn.record(_nd(valid_until=far), actor=AGENT)  # outside the window

        horizon = datetime.now(UTC) + timedelta(days=14)
        page = await bn.relevant(expiring_before=horizon)

        assert [d.id for d in page.items] == [expiring.decision_id]
        assert await bn.relevant_count(expiring_before=horizon) == 1


class TestQueueResolutionDelegation:
    async def test_dismiss_item_is_human_only(self, bn: Binnacle) -> None:
        source = await bn.record(_nd(), actor=AGENT)
        item_id = await bn.recommend(source.decision_id, actor=AGENT, reason=None)
        assert item_id is not None
        with pytest.raises(AuthorityViolation):
            await bn.dismiss_item(item_id, actor=AGENT, reason="noise")

    async def test_dismiss_item_resolves_without_mutating_decision(self, bn: Binnacle) -> None:
        source = await bn.record(_nd(), actor=AGENT)
        item_id = await bn.recommend(source.decision_id, actor=AGENT, reason=None)
        assert item_id is not None
        await bn.dismiss_item(item_id, actor=HUMAN, reason="noise")
        history = await bn.history(source.decision_id)
        assert history.decision.status == "current"
        assert any(t.action == "dismissed" for t in history.transitions)


class TestExport:
    """FR-6.6 (docs/binnacle-core/components/04's "Export content check"): `bn.export()`
    returns a JSON-safe dict -- `json.dumps` succeeds with no further
    conversion, embeddings never appear, the domains registry is included,
    and a spot re-hydration of one exported decision matches the stored
    `Decision` field-by-field."""

    async def test_export_document_is_json_serializable(self, bn: Binnacle) -> None:
        source = await bn.record(
            _nd(
                options_considered=[
                    OptionConsidered(option="fixed-interval retry", why_rejected="thundering herd")
                ],
                consequences="ops must monitor retry storms",
                confidence=0.8,
                valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                valid_until=datetime(2027, 1, 1, tzinfo=UTC),
                refs=[
                    Ref(role="subject", kind="component", identifier="portolan-ingest", note=None)
                ],
                metadata={"note": "from narrative walkthrough"},
            ),
            actor=AGENT,
        )
        item_id = await bn.recommend(source.decision_id, actor=AGENT, reason="ready")
        assert item_id is not None
        await bn.promote_refined([source.decision_id], refined=_nd(), actor=HUMAN)

        document = await bn.export()

        json.dumps(document)  # raises TypeError on anything not JSON-safe

    async def test_export_excludes_embeddings(self, bn: Binnacle) -> None:
        await bn.record(_nd(), actor=AGENT)

        document = await bn.export()

        assert "embeddings" not in document
        assert all("embedding" not in decision for decision in document["decisions"])

    async def test_export_includes_domains_registry(self, bn: Binnacle) -> None:
        await bn.add_domain("product", "product decisions", actor=HUMAN)

        document = await bn.export()

        names = {d["name"] for d in document["domains"]}
        assert {"eng", "product"} <= names

    async def test_export_spot_rehydration_equality(self, bn: Binnacle) -> None:
        """Parse one exported decision back and compare it, field-by-field,
        against the stored `Decision` (FR-6.6's "spot re-hydration
        equality")."""
        valid_from = datetime(2026, 1, 1, tzinfo=UTC)
        source = await bn.record(
            _nd(
                options_considered=[
                    OptionConsidered(option="fixed-interval retry", why_rejected="thundering herd")
                ],
                consequences="ops must monitor retry storms",
                confidence=0.8,
                valid_from=valid_from,
                refs=[
                    Ref(role="subject", kind="component", identifier="portolan-ingest", note=None)
                ],
                metadata={"note": "from narrative walkthrough"},
            ),
            actor=AGENT,
        )

        document = await bn.export()
        exported = next(
            d for d in document["decisions"] if d["decision_id"] == str(source.decision_id)
        )

        assert UUID(exported["decision_id"]) == source.decision_id
        assert exported["domain"] == source.domain
        assert exported["tier"] == source.tier
        assert exported["status"] == source.status
        assert exported["scenario"] == source.scenario
        assert exported["outcome"] == source.outcome
        assert exported["reasoning"] == source.reasoning
        assert exported["source"] == source.source
        assert Actor.from_str(exported["recorded_by"]) == source.recorded_by
        assert datetime.fromisoformat(exported["recorded_at"]) == source.recorded_at
        assert datetime.fromisoformat(exported["valid_from"]) == source.valid_from
        assert exported["valid_until"] is None
        assert exported["consequences"] == source.consequences
        assert exported["confidence"] == source.confidence
        assert exported["options_considered"] == [
            {"option": "fixed-interval retry", "why_rejected": "thundering herd"}
        ]
        assert exported["refs"] == [
            {"role": "subject", "kind": "component", "identifier": "portolan-ingest", "note": None}
        ]
        assert exported["metadata"] == {"note": "from narrative walkthrough"}
        assert exported["schema_version"] == source.schema_version

    async def test_export_filters_by_domain(self, bn: Binnacle) -> None:
        await bn.add_domain("product", "product decisions", actor=HUMAN)
        eng_decision = await bn.record(_nd(domain="eng"), actor=AGENT)
        await bn.record(_nd(domain="product"), actor=AGENT)

        document = await bn.export(domains=["eng"])

        exported_ids = {d["decision_id"] for d in document["decisions"]}
        assert exported_ids == {str(eng_decision.decision_id)}
