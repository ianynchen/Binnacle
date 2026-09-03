"""Integration tests for the `Binnacle` public client (needs a live Postgres;
see conftest.pg_dsn). docs/components/01-configuration-and-client.md's
"Acceptance" is the spec these tests mirror: an embedder-shaped test drives
record -> recommend -> promote_refined -> query -> export end-to-end through
the public API only, plus the two boundary-error cases named there.

This module exercises `Binnacle` itself (config wiring, actor passthrough,
registry verbs it owns outright) — it does NOT re-litigate Lifecycle Engine
state-machine coverage, already exhaustive in tests/db/test_lifecycle.py.
"""

from collections.abc import AsyncIterator

import pytest

from binnacle.application.config import BinnacleConfig
from binnacle.client import Binnacle
from binnacle.domain.errors import AuthorityViolation, UnknownDomain
from binnacle.domain.models import Actor, NewDecision
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
    return NewDecision(**base)  # type: ignore[arg-type]


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
    """docs/components/01 "Acceptance": record (agent) -> recommend ->
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
        assert any(d.id == refined.decision_id for d in compact)  # type: ignore[union-attr]

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

        # export: the refined long-term decision and its source both appear.
        bundle = await bn.export()
        exported_ids = {d.decision_id for d in bundle.decisions}
        assert {source.decision_id, refined.decision_id} <= exported_ids
        assert any(dom.name == "eng" for dom in bundle.domains)


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
        assert compact  # type: ignore[truthy-bool]
        assert all(len(d.outcome_truncated) <= 5 for d in compact)  # type: ignore[union-attr]
        await client.aclose()

    async def test_relevant_full_projection_untruncated(self, bn: Binnacle) -> None:
        long_outcome = "x" * 500
        await bn.record(_nd(outcome=long_outcome), actor=AGENT)
        full = await bn.relevant(domains=["eng"], projection="full")
        assert any(d.outcome == long_outcome for d in full)  # type: ignore[union-attr]

    async def test_queue_returns_open_recommendation(self, bn: Binnacle) -> None:
        source = await bn.record(_nd(), actor=AGENT)
        await bn.recommend(source.decision_id, actor=AGENT, reason="ready")
        open_items = await bn.queue(kinds=["promote"])
        assert any(v.item.decision_id == source.decision_id for v in open_items)

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
