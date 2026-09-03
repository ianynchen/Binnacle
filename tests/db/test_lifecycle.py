"""Integration tests for LifecycleEngine (needs a live Postgres; see
conftest.pg_dsn). docs/components/03-lifecycle-engine.md's exit-matrix and acts
tables ARE the spec these tests mirror; REQUIREMENTS FR-3/4/5 resolve any cell
03 leaves ambiguous.

Layout:
- TestRecordAndRecordLongTerm: authority, UnknownDomain, idempotency for the two
  recording acts (no "from status" — they create rows, not transition them).
- TestMatrix*: the exhaustive (act x actor-kind x from-status) tables, one class
  per act, each parametrize table mirroring 03's rows.
- test_property_random_walk: a random legal walk over >=30 decisions, >=200
  acts, asserting the I-1 fold invariant and content immutability hold for
  every decision afterward.
- TestTargeted: refined multi-source consolidation, pending-LT-claim execution,
  cross-tier supersede refusal, cycle refusal, the promote-vs-supersede race,
  and recommend-on-archived's implicit reactivation.

A note on why some item-driven acts' "terminal status" matrix cells are absent:
every act that moves a decision OUT of current/not_promoted (discard, supersede,
archive-refusal aside) also auto-voids that decision's open queue items in the
same transaction (FR-4.3). So a `promote`/`decline` queue item can never remain
open while its decision has already drifted to a terminal status via normal,
sequential use — that specific cell is only reachable through a genuine
concurrent race, which `TestTargeted::test_promote_vs_supersede_race` exercises
directly instead of being faked here.
"""

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest

from binnacle.adapters.postgres_store import PostgresStore
from binnacle.application.lifecycle import LifecycleEngine
from binnacle.domain.errors import (
    AuthorityViolation,
    DecisionNotFound,
    IdempotencyConflict,
    InvalidTransition,
    ItemAlreadyResolved,
    ItemNotFound,
    UnknownDomain,
)
from binnacle.domain.models import Actor, Decision, NewDecision, Transition

HUMAN = Actor("human", "alice")
RECORDER = Actor("agent", "meridian/recorder")
OTHER_AGENT = Actor("agent", "meridian/other")
ENGINE_ACTOR = Actor("engine", "binnacle")


@pytest.fixture()
async def store(pg_dsn: str, scratch_schema: str) -> AsyncIterator[PostgresStore]:
    s = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=768)
    await s.migrate()
    async with s.transaction() as tx:
        await s.upsert_domain(
            tx, "eng", "engineering", True, HUMAN.as_str(), "domain_created", None
        )
    yield s
    await s.aclose()


@pytest.fixture()
def engine(store: PostgresStore) -> LifecycleEngine:
    return LifecycleEngine(store)


def _nd(**overrides: Any) -> NewDecision:
    base: dict[str, Any] = {
        "domain": "eng",
        "scenario": "adopt exponential backoff",
        "outcome": "retries use exponential backoff",
        "reasoning": "reduces thundering herd",
        "source": "test-suite",
    }
    base.update(overrides)
    return NewDecision(**base)


def _fold(transitions: list[Transition]) -> str | None:
    """I-1: a decision's status is the last non-null `new_status` in its
    transitions, in (at, transition_id) order — the same order `history()`
    already returns them in."""
    non_null = [t.new_status for t in transitions if t.new_status is not None]
    return non_null[-1] if non_null else None


async def _status(store: PostgresStore, decision_id: UUID) -> str:
    d = await store.get_decision(decision_id)
    assert d is not None
    return d.status


async def reach(
    engine: LifecycleEngine,
    store: PostgresStore,
    tier: str,
    status: str,
    *,
    recorded_by: Actor = RECORDER,
) -> Decision:
    """Build a decision in EXACTLY `(tier, status)`, using only legal engine acts
    (never raw store writes) — so every matrix test's setup is itself proof the
    state is reachable through the public API."""
    if tier == "short_term":
        d = await engine.record(_nd(), recorded_by)
        if status == "current":
            return d
        if status == "not_promoted":
            item_id = await engine.recommend(d.decision_id, recorded_by, "consider")
            assert item_id is not None
            await engine.decline(item_id, HUMAN, "not yet")
        elif status == "promoted":
            item_id = await engine.recommend(d.decision_id, recorded_by, "consider")
            assert item_id is not None
            await engine.promote(item_id, HUMAN)
        elif status == "superseded":
            successor = await engine.record(_nd(scenario="successor"), recorded_by)
            await engine.supersede(successor.decision_id, d.decision_id, recorded_by)
        elif status == "discarded":
            await engine.discard(d.decision_id, recorded_by, "junk")
        elif status == "archived":
            await engine.archive([d.decision_id], ENGINE_ACTOR)
        else:
            raise ValueError(f"unreachable short_term status: {status}")
    else:
        d = await engine.record_long_term(_nd(), HUMAN)
        if status == "current":
            return d
        if status == "superseded":
            successor = await engine.record_long_term(_nd(scenario="successor"), HUMAN)
            await engine.supersede(successor.decision_id, d.decision_id, HUMAN)
        else:
            raise ValueError(f"unreachable long_term status: {status}")
    refetched = await store.get_decision(d.decision_id)
    assert refetched is not None
    return refetched


async def expect(call: Callable[[], Awaitable[Any]], expected: type[BaseException] | None) -> Any:
    """Run `call()`; assert it either succeeds (expected=None) or raises exactly
    `expected`."""
    if expected is None:
        return await call()
    with pytest.raises(expected):
        await call()
    return None


# ===========================================================================
# record / record_long_term — no "from status" (they create rows)
# ===========================================================================


class TestRecordAndRecordLongTerm:
    async def test_record_allows_any_actor(self, engine: LifecycleEngine) -> None:
        for actor in (HUMAN, RECORDER, ENGINE_ACTOR):
            d = await engine.record(_nd(), actor)
            assert d.status == "current"
            assert d.tier == "short_term"

    async def test_record_unknown_domain_raises(self, engine: LifecycleEngine) -> None:
        with pytest.raises(UnknownDomain):
            await engine.record(_nd(domain="nonexistent"), RECORDER)

    async def test_record_identical_retry_is_a_noop(self, engine: LifecycleEngine) -> None:
        from uuid import uuid4

        fixed_id = uuid4()
        first = await engine.record(_nd(decision_id=fixed_id), RECORDER)
        second = await engine.record(_nd(decision_id=fixed_id), RECORDER)
        assert first.decision_id == second.decision_id == fixed_id
        assert first.recorded_at == second.recorded_at

    async def test_record_long_term_requires_human(self, engine: LifecycleEngine) -> None:
        d = await engine.record_long_term(_nd(), HUMAN)
        assert d.tier == "long_term"
        assert d.status == "current"
        for actor in (RECORDER, ENGINE_ACTOR):
            with pytest.raises(AuthorityViolation):
                await engine.record_long_term(_nd(), actor)

    async def test_record_long_term_writes_recorded_and_promoted_transitions(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        d = await engine.record_long_term(_nd(), HUMAN)
        hist = await store.history(d.decision_id)
        actions = [t.action for t in hist.transitions]
        assert actions == ["recorded", "promoted"]
        assert _fold(hist.transitions) == "current"

    async def test_record_long_term_unknown_domain_raises(self, engine: LifecycleEngine) -> None:
        with pytest.raises(UnknownDomain):
            await engine.record_long_term(_nd(domain="nonexistent"), HUMAN)

    async def test_cross_tier_id_reuse_is_not_idempotent(self, engine: LifecycleEngine) -> None:
        """`content_hash` never covers `tier` — reusing an id across `record`
        (short-term) and `record_long_term` with otherwise-identical content
        must not silently return the wrong-tier row as if it were the same
        idempotent request."""
        from uuid import uuid4

        fixed_id = uuid4()
        await engine.record(_nd(decision_id=fixed_id), RECORDER)
        with pytest.raises(IdempotencyConflict):
            await engine.record_long_term(_nd(decision_id=fixed_id), HUMAN)


class TestRecordDeclaredSupplements:
    """FR-1.4: `supplements=` declared at record time — symmetric to
    `supersedes` handling above (FR-5's authority rule): short-term targets
    link inline with NO status change (FR-5.3); long-term targets file a
    pending `queue(kind='link')` item for a human to execute later via
    `apply_item` (I-2)."""

    async def test_short_term_target_links_inline(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        target = await engine.record(_nd(scenario="base decision"), RECORDER)
        new = await engine.record(
            _nd(scenario="supplementary detail", supplements=[target.decision_id]), RECORDER
        )
        assert await _status(store, target.decision_id) == "current"
        target_hist = await store.history(target.decision_id)
        new_hist = await store.history(new.decision_id)
        assert "supplement_linked" in [t.action for t in target_hist.transitions]
        assert "supplement_linked" in [t.action for t in new_hist.transitions]
        assert any(
            link.kind == "SUPPLEMENTS"
            and link.from_id == new.decision_id
            and link.to_id == target.decision_id
            for link in target_hist.links
        )

    async def test_long_term_target_files_pending_link_item(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        target = await engine.record_long_term(_nd(scenario="durable policy"), HUMAN)
        new = await engine.record(
            _nd(scenario="agent notes a supplement", supplements=[target.decision_id]), RECORDER
        )
        # A long-term target only gets a pending claim (I-2) — no link yet.
        target_hist = await store.history(target.decision_id)
        assert target_hist.links == []
        assert await _status(store, target.decision_id) == "current"

        open_items = await store.open_queue(kinds=["link"])
        matching = [
            v
            for v in open_items
            if v.item.decision_id == new.decision_id and v.item.target_id == target.decision_id
        ]
        assert len(matching) == 1
        item_id = matching[0].item.item_id

        await engine.apply_item(item_id, HUMAN)
        target_hist = await store.history(target.decision_id)
        assert any(
            link.kind == "SUPPLEMENTS" and link.from_id == new.decision_id
            for link in target_hist.links
        )

    async def test_mixed_supersedes_and_supplements_in_one_record(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        supersede_target = await engine.record(_nd(scenario="stale decision"), RECORDER)
        supplement_target = await engine.record(_nd(scenario="related decision"), RECORDER)
        new = await engine.record(
            _nd(
                scenario="consolidated decision",
                supersedes=[supersede_target.decision_id],
                supplements=[supplement_target.decision_id],
            ),
            RECORDER,
        )
        assert await _status(store, supersede_target.decision_id) == "superseded"
        assert await _status(store, supplement_target.decision_id) == "current"
        new_hist = await store.history(new.decision_id)
        actions = [t.action for t in new_hist.transitions]
        assert "superseded" in actions
        assert "supplement_linked" in actions


# ===========================================================================
# recommend — any actor; archived implicitly reactivates
# ===========================================================================


class TestMatrixRecommend:
    @pytest.mark.parametrize(
        ("tier", "status", "expected"),
        [
            ("short_term", "current", None),
            ("short_term", "not_promoted", None),
            ("short_term", "archived", None),
            ("short_term", "promoted", InvalidTransition),
            ("short_term", "superseded", InvalidTransition),
            ("short_term", "discarded", InvalidTransition),
            ("long_term", "current", InvalidTransition),
            ("long_term", "superseded", InvalidTransition),
        ],
    )
    @pytest.mark.parametrize("actor", [HUMAN, RECORDER, ENGINE_ACTOR])
    async def test_recommend_matrix(
        self,
        engine: LifecycleEngine,
        store: PostgresStore,
        tier: str,
        status: str,
        actor: Actor,
        expected: type[BaseException] | None,
    ) -> None:
        d = await reach(engine, store, tier, status)
        result = await expect(lambda: engine.recommend(d.decision_id, actor, "why"), expected)
        if expected is None:
            assert isinstance(result, int)

    async def test_recommend_unknown_decision_raises(self, engine: LifecycleEngine) -> None:
        from uuid import uuid4

        with pytest.raises(DecisionNotFound):
            await engine.recommend(uuid4(), RECORDER, "why")


# ===========================================================================
# promote / decline — human only; item-driven
# ===========================================================================


class TestMatrixPromote:
    @pytest.mark.parametrize("status", ["current", "not_promoted"])
    async def test_human_succeeds(
        self, engine: LifecycleEngine, store: PostgresStore, status: str
    ) -> None:
        d = await reach(engine, store, "short_term", status)
        item_id = await engine.recommend(d.decision_id, RECORDER, "why")
        assert item_id is not None
        copy = await engine.promote(item_id, HUMAN)
        assert copy.tier == "long_term"
        assert copy.status == "current"
        assert await _status(store, d.decision_id) == "promoted"

    @pytest.mark.parametrize("actor", [RECORDER, ENGINE_ACTOR])
    async def test_non_human_refused(
        self, engine: LifecycleEngine, store: PostgresStore, actor: Actor
    ) -> None:
        d = await reach(engine, store, "short_term", "current")
        item_id = await engine.recommend(d.decision_id, RECORDER, "why")
        assert item_id is not None
        with pytest.raises(AuthorityViolation):
            await engine.promote(item_id, actor)

    async def test_wrong_item_kind_refused(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        lt = await reach(engine, store, "long_term", "current")
        d = await reach(engine, store, "short_term", "current")
        async with store.transaction() as tx:
            item_id = await store.enqueue(
                tx, "supersede", d.decision_id, lt.decision_id, RECORDER, None, None
            )
        assert item_id is not None
        with pytest.raises(InvalidTransition):
            await engine.promote(item_id, HUMAN)

    async def test_unknown_item_raises_item_not_found(self, engine: LifecycleEngine) -> None:
        with pytest.raises(ItemNotFound):
            await engine.promote(999_999_999, HUMAN)

    async def test_promote_voids_stray_open_items_on_source(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        """`promoted` is terminal — a second, unrelated open item left on the
        source must be voided too (I-4), not just the triggering promote item."""
        d = await reach(engine, store, "short_term", "current")
        promote_item_id = await engine.recommend(d.decision_id, RECORDER, "promote me")
        assert promote_item_id is not None
        other = await engine.record(_nd(scenario="unrelated candidate"), RECORDER)
        async with store.transaction() as tx:
            stray_item_id = await store.enqueue(
                tx, "link", d.decision_id, other.decision_id, RECORDER, "maybe related", 0.5
            )
        assert stray_item_id is not None

        await engine.promote(promote_item_id, HUMAN)

        with pytest.raises(ItemAlreadyResolved):
            await engine.apply_item(stray_item_id, HUMAN)
        hist = await store.history(d.decision_id)
        voided_item_ids = {
            t.payload["item_id"]
            for t in hist.transitions
            if t.action == "voided" and t.payload is not None
        }
        assert stray_item_id in voided_item_ids


class TestMatrixDecline:
    @pytest.mark.parametrize("status", ["current", "not_promoted"])
    async def test_human_succeeds(
        self, engine: LifecycleEngine, store: PostgresStore, status: str
    ) -> None:
        d = await reach(engine, store, "short_term", status)
        item_id = await engine.recommend(d.decision_id, RECORDER, "why")
        assert item_id is not None
        await engine.decline(item_id, HUMAN, "not ready")
        assert await _status(store, d.decision_id) == "not_promoted"

    @pytest.mark.parametrize("actor", [RECORDER, ENGINE_ACTOR])
    async def test_non_human_refused(
        self, engine: LifecycleEngine, store: PostgresStore, actor: Actor
    ) -> None:
        d = await reach(engine, store, "short_term", "current")
        item_id = await engine.recommend(d.decision_id, RECORDER, "why")
        assert item_id is not None
        with pytest.raises(AuthorityViolation):
            await engine.decline(item_id, actor, "no")


# ===========================================================================
# promote_refined — human only; sources checked directly (reachable matrix)
# ===========================================================================


class TestMatrixPromoteRefined:
    @pytest.mark.parametrize(
        ("tier", "status", "expected"),
        [
            ("short_term", "current", None),
            ("short_term", "not_promoted", None),
            ("short_term", "promoted", InvalidTransition),
            ("short_term", "superseded", InvalidTransition),
            ("short_term", "discarded", InvalidTransition),
            ("short_term", "archived", InvalidTransition),
            ("long_term", "current", InvalidTransition),
        ],
    )
    async def test_source_status_matrix(
        self,
        engine: LifecycleEngine,
        store: PostgresStore,
        tier: str,
        status: str,
        expected: type[BaseException] | None,
    ) -> None:
        source = await reach(engine, store, tier, status)
        result = await expect(
            lambda: engine.promote_refined([source.decision_id], _nd(scenario="refined"), HUMAN),
            expected,
        )
        if expected is None:
            assert result.tier == "long_term"

    @pytest.mark.parametrize("actor", [RECORDER, ENGINE_ACTOR])
    async def test_non_human_refused(
        self, engine: LifecycleEngine, store: PostgresStore, actor: Actor
    ) -> None:
        source = await reach(engine, store, "short_term", "current")
        with pytest.raises(AuthorityViolation):
            await engine.promote_refined([source.decision_id], _nd(), actor)

    async def test_empty_sources_raises_value_error(self, engine: LifecycleEngine) -> None:
        with pytest.raises(ValueError, match="at least one source"):
            await engine.promote_refined([], _nd(), HUMAN)


# ===========================================================================
# discard — FR-3.3 recorder-of-own-current or human
# ===========================================================================


class TestMatrixDiscard:
    async def test_recorder_may_discard_own_current(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        d = await reach(engine, store, "short_term", "current")
        await engine.discard(d.decision_id, RECORDER, "junk")
        assert await _status(store, d.decision_id) == "discarded"

    async def test_human_may_discard_any_current(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        d = await reach(engine, store, "short_term", "current")
        await engine.discard(d.decision_id, HUMAN, "junk")
        assert await _status(store, d.decision_id) == "discarded"

    async def test_non_recorder_agent_refused_on_current(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        d = await reach(engine, store, "short_term", "current")
        with pytest.raises(AuthorityViolation):
            await engine.discard(d.decision_id, OTHER_AGENT, "junk")

    @pytest.mark.parametrize("status", ["not_promoted", "archived"])
    async def test_recorder_refused_off_current(
        self, engine: LifecycleEngine, store: PostgresStore, status: str
    ) -> None:
        d = await reach(engine, store, "short_term", status)
        with pytest.raises(AuthorityViolation):
            await engine.discard(d.decision_id, RECORDER, "junk")

    @pytest.mark.parametrize("status", ["not_promoted", "archived"])
    async def test_human_may_discard_off_current(
        self, engine: LifecycleEngine, store: PostgresStore, status: str
    ) -> None:
        d = await reach(engine, store, "short_term", status)
        await engine.discard(d.decision_id, HUMAN, "junk")
        assert await _status(store, d.decision_id) == "discarded"

    @pytest.mark.parametrize("status", ["promoted", "superseded", "discarded"])
    async def test_terminal_statuses_refused(
        self, engine: LifecycleEngine, store: PostgresStore, status: str
    ) -> None:
        d = await reach(engine, store, "short_term", status)
        with pytest.raises(InvalidTransition):
            await engine.discard(d.decision_id, HUMAN, "junk")

    async def test_long_term_never_discardable(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        d = await reach(engine, store, "long_term", "current")
        with pytest.raises(InvalidTransition):
            await engine.discard(d.decision_id, HUMAN, "junk")

    async def test_discard_voids_open_items(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        d = await reach(engine, store, "short_term", "current")
        item_id = await engine.recommend(d.decision_id, RECORDER, "why")
        assert item_id is not None
        await engine.discard(d.decision_id, HUMAN, "actually junk")
        with pytest.raises(ItemAlreadyResolved):
            await engine.decline(item_id, HUMAN, "too late")


# ===========================================================================
# supersede — FR-5.2a tier symmetry
# ===========================================================================


class TestMatrixSupersede:
    @pytest.mark.parametrize("status", ["current", "not_promoted"])
    @pytest.mark.parametrize("actor", [HUMAN, RECORDER, ENGINE_ACTOR])
    async def test_short_term_ungated(
        self, engine: LifecycleEngine, store: PostgresStore, status: str, actor: Actor
    ) -> None:
        old = await reach(engine, store, "short_term", status)
        new = await engine.record(_nd(scenario="successor"), RECORDER)
        await engine.supersede(new.decision_id, old.decision_id, actor)
        assert await _status(store, old.decision_id) == "superseded"

    @pytest.mark.parametrize("status", ["promoted", "superseded", "discarded", "archived"])
    async def test_short_term_terminal_refused(
        self, engine: LifecycleEngine, store: PostgresStore, status: str
    ) -> None:
        old = await reach(engine, store, "short_term", status)
        new = await engine.record(_nd(scenario="successor"), RECORDER)
        with pytest.raises(InvalidTransition):
            await engine.supersede(new.decision_id, old.decision_id, RECORDER)

    async def test_long_term_requires_human_and_long_term_successor(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        old = await reach(engine, store, "long_term", "current")
        new = await engine.record_long_term(_nd(scenario="successor"), HUMAN)
        await engine.supersede(new.decision_id, old.decision_id, HUMAN)
        assert await _status(store, old.decision_id) == "superseded"

    @pytest.mark.parametrize("actor", [RECORDER, ENGINE_ACTOR])
    async def test_long_term_non_human_refused(
        self, engine: LifecycleEngine, store: PostgresStore, actor: Actor
    ) -> None:
        old = await reach(engine, store, "long_term", "current")
        new = await engine.record_long_term(_nd(scenario="successor"), HUMAN)
        with pytest.raises(AuthorityViolation):
            await engine.supersede(new.decision_id, old.decision_id, actor)

    async def test_long_term_terminal_refused(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        old = await reach(engine, store, "long_term", "superseded")
        new = await engine.record_long_term(_nd(scenario="another"), HUMAN)
        with pytest.raises(InvalidTransition):
            await engine.supersede(new.decision_id, old.decision_id, HUMAN)

    async def test_unknown_old_raises_decision_not_found(self, engine: LifecycleEngine) -> None:
        from uuid import uuid4

        new = await engine.record(_nd(), RECORDER)
        with pytest.raises(DecisionNotFound):
            await engine.supersede(new.decision_id, uuid4(), RECORDER)

    async def test_supersede_voids_target_open_items(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        old = await reach(engine, store, "short_term", "current")
        item_id = await engine.recommend(old.decision_id, RECORDER, "why")
        assert item_id is not None
        new = await engine.record(_nd(scenario="successor"), RECORDER)
        await engine.supersede(new.decision_id, old.decision_id, RECORDER)
        with pytest.raises(ItemAlreadyResolved):
            await engine.decline(item_id, HUMAN, "too late")


# ===========================================================================
# supplement — no status change; human gate only when target is long_term
# ===========================================================================


class TestMatrixSupplement:
    @pytest.mark.parametrize("actor", [HUMAN, RECORDER, ENGINE_ACTOR])
    async def test_short_term_target_ungated(
        self, engine: LifecycleEngine, store: PostgresStore, actor: Actor
    ) -> None:
        old = await reach(engine, store, "short_term", "current")
        new = await engine.record(_nd(scenario="supplementary"), RECORDER)
        await engine.supplement(new.decision_id, old.decision_id, actor)
        assert await _status(store, old.decision_id) == "current"

    async def test_long_term_target_requires_human(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        old = await reach(engine, store, "long_term", "current")
        new = await engine.record_long_term(_nd(scenario="supplementary"), HUMAN)
        await engine.supplement(new.decision_id, old.decision_id, HUMAN)
        assert await _status(store, old.decision_id) == "current"

    @pytest.mark.parametrize("actor", [RECORDER, ENGINE_ACTOR])
    async def test_long_term_target_non_human_refused(
        self, engine: LifecycleEngine, store: PostgresStore, actor: Actor
    ) -> None:
        old = await reach(engine, store, "long_term", "current")
        new = await engine.record_long_term(_nd(scenario="supplementary"), HUMAN)
        with pytest.raises(AuthorityViolation):
            await engine.supplement(new.decision_id, old.decision_id, actor)

    async def test_writes_supplement_linked_both_sides(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        old = await reach(engine, store, "short_term", "current")
        new = await engine.record(_nd(scenario="supplementary"), RECORDER)
        await engine.supplement(new.decision_id, old.decision_id, RECORDER)
        old_hist = await store.history(old.decision_id)
        new_hist = await store.history(new.decision_id)
        assert "supplement_linked" in [t.action for t in old_hist.transitions]
        assert "supplement_linked" in [t.action for t in new_hist.transitions]


# ===========================================================================
# reactivate — any actor; only from archived
# ===========================================================================


class TestMatrixReactivate:
    @pytest.mark.parametrize("prior_status", ["current", "not_promoted"])
    @pytest.mark.parametrize("actor", [HUMAN, RECORDER, ENGINE_ACTOR])
    async def test_restores_prior_status(
        self,
        engine: LifecycleEngine,
        store: PostgresStore,
        prior_status: str,
        actor: Actor,
    ) -> None:
        d = await reach(engine, store, "short_term", prior_status)
        await engine.archive([d.decision_id], ENGINE_ACTOR)
        await engine.reactivate(d.decision_id, actor)
        assert await _status(store, d.decision_id) == prior_status

    @pytest.mark.parametrize(
        ("tier", "status"),
        [
            ("short_term", "current"),
            ("short_term", "promoted"),
            ("short_term", "superseded"),
            ("short_term", "discarded"),
            ("long_term", "current"),
        ],
    )
    async def test_non_archived_refused(
        self, engine: LifecycleEngine, store: PostgresStore, tier: str, status: str
    ) -> None:
        d = await reach(engine, store, tier, status)
        with pytest.raises(InvalidTransition):
            await engine.reactivate(d.decision_id, HUMAN)


# ===========================================================================
# archive — engine or human; blocked by open items; all-or-nothing
# ===========================================================================


class TestMatrixArchive:
    @pytest.mark.parametrize("actor", [HUMAN, ENGINE_ACTOR])
    @pytest.mark.parametrize("status", ["current", "not_promoted"])
    async def test_succeeds(
        self, engine: LifecycleEngine, store: PostgresStore, status: str, actor: Actor
    ) -> None:
        d = await reach(engine, store, "short_term", status)
        count = await engine.archive([d.decision_id], actor)
        assert count == 1
        assert await _status(store, d.decision_id) == "archived"

    async def test_agent_refused(self, engine: LifecycleEngine, store: PostgresStore) -> None:
        d = await reach(engine, store, "short_term", "current")
        with pytest.raises(AuthorityViolation):
            await engine.archive([d.decision_id], RECORDER)

    @pytest.mark.parametrize(
        ("tier", "status"),
        [
            ("short_term", "promoted"),
            ("short_term", "superseded"),
            ("short_term", "discarded"),
            ("short_term", "archived"),
            ("long_term", "current"),
        ],
    )
    async def test_ineligible_status_refused(
        self, engine: LifecycleEngine, store: PostgresStore, tier: str, status: str
    ) -> None:
        d = await reach(engine, store, tier, status)
        with pytest.raises(InvalidTransition):
            await engine.archive([d.decision_id], ENGINE_ACTOR)

    async def test_open_items_block_archival(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        d = await reach(engine, store, "short_term", "current")
        item_id = await engine.recommend(d.decision_id, RECORDER, "why")
        assert item_id is not None
        with pytest.raises(InvalidTransition):
            await engine.archive([d.decision_id], ENGINE_ACTOR)

    async def test_partial_failure_archives_nothing(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        ok = await reach(engine, store, "short_term", "current")
        bad = await reach(engine, store, "short_term", "discarded")
        with pytest.raises(InvalidTransition):
            await engine.archive([ok.decision_id, bad.decision_id], ENGINE_ACTOR)
        assert await _status(store, ok.decision_id) == "current"


# ===========================================================================
# apply_item — human always; kind in {link, supersede}
# ===========================================================================


class TestMatrixApplyItem:
    async def test_supersede_kind_human_succeeds(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        old = await reach(engine, store, "short_term", "current")
        new = await engine.record(_nd(scenario="candidate successor"), RECORDER)
        async with store.transaction() as tx:
            item_id = await store.enqueue(
                tx, "supersede", new.decision_id, old.decision_id, RECORDER, "looks similar", 0.9
            )
        assert item_id is not None
        await engine.apply_item(item_id, HUMAN)
        assert await _status(store, old.decision_id) == "superseded"

    async def test_link_kind_human_succeeds(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        old = await reach(engine, store, "short_term", "current")
        new = await engine.record(_nd(scenario="candidate supplement"), RECORDER)
        async with store.transaction() as tx:
            item_id = await store.enqueue(
                tx, "link", new.decision_id, old.decision_id, RECORDER, "related", 0.7
            )
        assert item_id is not None
        await engine.apply_item(item_id, HUMAN)
        old_hist = await store.history(old.decision_id)
        assert "supplement_linked" in [t.action for t in old_hist.transitions]
        assert await _status(store, old.decision_id) == "current"

    @pytest.mark.parametrize("actor", [RECORDER, ENGINE_ACTOR])
    async def test_non_human_refused(
        self, engine: LifecycleEngine, store: PostgresStore, actor: Actor
    ) -> None:
        old = await reach(engine, store, "short_term", "current")
        new = await engine.record(_nd(scenario="candidate"), RECORDER)
        async with store.transaction() as tx:
            item_id = await store.enqueue(
                tx, "link", new.decision_id, old.decision_id, RECORDER, None, None
            )
        assert item_id is not None
        with pytest.raises(AuthorityViolation):
            await engine.apply_item(item_id, actor)

    async def test_wrong_kind_refused(self, engine: LifecycleEngine, store: PostgresStore) -> None:
        d = await reach(engine, store, "short_term", "current")
        item_id = await engine.recommend(d.decision_id, RECORDER, "why")
        assert item_id is not None
        with pytest.raises(InvalidTransition):
            await engine.apply_item(item_id, HUMAN)

    async def test_stale_target_status_refused(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        """A 'supersede' suggestion fabricated directly against the store (as
        discovery would) against a target that has since gone terminal —
        reachable here because we bypassed the engine's own auto-void when
        creating the item."""
        old = await reach(engine, store, "short_term", "discarded")
        new = await engine.record(_nd(scenario="candidate"), RECORDER)
        async with store.transaction() as tx:
            item_id = await store.enqueue(
                tx, "supersede", new.decision_id, old.decision_id, RECORDER, None, None
            )
        assert item_id is not None
        with pytest.raises(InvalidTransition):
            await engine.apply_item(item_id, HUMAN)


# ===========================================================================
# dismiss_item — human always; record untouched
# ===========================================================================


class TestMatrixDismissItem:
    async def test_human_succeeds_and_leaves_decision_untouched(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        d = await reach(engine, store, "short_term", "current")
        item_id = await engine.recommend(d.decision_id, RECORDER, "why")
        assert item_id is not None
        await engine.dismiss_item(item_id, HUMAN, "not relevant")
        assert await _status(store, d.decision_id) == "current"
        hist = await store.history(d.decision_id)
        assert "dismissed" in [t.action for t in hist.transitions]

    @pytest.mark.parametrize("actor", [RECORDER, ENGINE_ACTOR])
    async def test_non_human_refused(
        self, engine: LifecycleEngine, store: PostgresStore, actor: Actor
    ) -> None:
        d = await reach(engine, store, "short_term", "current")
        item_id = await engine.recommend(d.decision_id, RECORDER, "why")
        assert item_id is not None
        with pytest.raises(AuthorityViolation):
            await engine.dismiss_item(item_id, actor, "no")

    async def test_double_dismiss_raises_item_already_resolved(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        d = await reach(engine, store, "short_term", "current")
        item_id = await engine.recommend(d.decision_id, RECORDER, "why")
        assert item_id is not None
        await engine.dismiss_item(item_id, HUMAN, "not relevant")
        with pytest.raises(ItemAlreadyResolved):
            await engine.dismiss_item(item_id, HUMAN, "again")


# ===========================================================================
# Property test (Step 2): a random legal walk, then I-1 fold + content check.
# ===========================================================================


class _Tracked:
    __slots__ = (
        "decision_id",
        "open_promote_item",
        "pre_archive_status",
        "recorded_by",
        "scenario",
        "seq",
        "status",
        "tier",
    )

    def __init__(
        self, decision_id: UUID, tier: str, status: str, recorded_by: Actor, seq: int, scenario: str
    ) -> None:
        self.decision_id = decision_id
        self.tier = tier
        self.status = status
        self.recorded_by = recorded_by
        self.seq = seq
        self.open_promote_item: int | None = None
        self.pre_archive_status: str | None = None
        self.scenario = scenario


async def test_property_random_walk(engine: LifecycleEngine, store: PostgresStore) -> None:
    """>=200 acts across >=30 decisions, including promotions, refinements,
    supersedes, and archivals — asserting I-1 (status == fold(transitions)) and
    content immutability hold for every decision at the end.

    Every act attempted here is chosen to be legal FOR THE TEST DRIVER'S OWN
    tracked state — an exception from the engine mid-walk is a genuine bug
    (the driver's tracked state and the engine's state-machine understanding
    have diverged), so nothing here is wrapped in `pytest.raises`.
    """
    rng = random.Random(20260903)
    pool: list[_Tracked] = []
    seq_counter = 0
    acts_run = 0
    target_acts = 220
    min_pool = 30

    async def do_record() -> None:
        nonlocal seq_counter
        actor = rng.choice([HUMAN, RECORDER])
        scenario = f"walk-scenario-{seq_counter}"
        d = await engine.record(_nd(scenario=scenario), actor)
        pool.append(_Tracked(d.decision_id, "short_term", "current", actor, seq_counter, scenario))
        seq_counter += 1

    async def do_recommend(d: _Tracked) -> None:
        actor = rng.choice([HUMAN, RECORDER, ENGINE_ACTOR])
        item_id = await engine.recommend(d.decision_id, actor, "walk-reason")
        if d.status == "archived":
            assert d.pre_archive_status is not None
            d.status = d.pre_archive_status
            d.pre_archive_status = None
        d.open_promote_item = item_id

    async def do_promote(d: _Tracked) -> None:
        nonlocal seq_counter
        assert d.open_promote_item is not None
        lt = await engine.promote(d.open_promote_item, HUMAN)
        d.status = "promoted"
        d.open_promote_item = None
        pool.append(
            _Tracked(lt.decision_id, "long_term", "current", HUMAN, seq_counter, lt.scenario)
        )
        seq_counter += 1

    async def do_promote_refined(sources: list[_Tracked]) -> None:
        nonlocal seq_counter
        scenario = f"walk-refined-{seq_counter}"
        lt = await engine.promote_refined(
            [s.decision_id for s in sources], _nd(scenario=scenario), HUMAN
        )
        for s in sources:
            s.status = "promoted"
            s.open_promote_item = None
        pool.append(_Tracked(lt.decision_id, "long_term", "current", HUMAN, seq_counter, scenario))
        seq_counter += 1

    async def do_decline(d: _Tracked) -> None:
        assert d.open_promote_item is not None
        await engine.decline(d.open_promote_item, HUMAN, "walk-decline")
        d.status = "not_promoted"
        d.open_promote_item = None

    async def do_discard(d: _Tracked) -> None:
        actor = d.recorded_by if d.status == "current" and rng.random() < 0.5 else HUMAN
        await engine.discard(d.decision_id, actor, "walk-discard")
        d.status = "discarded"
        d.open_promote_item = None

    async def do_supersede(old: _Tracked, new: _Tracked) -> None:
        actor = HUMAN if old.tier == "long_term" else rng.choice([HUMAN, RECORDER, ENGINE_ACTOR])
        await engine.supersede(new.decision_id, old.decision_id, actor)
        old.status = "superseded"
        old.open_promote_item = None

    async def do_supplement(old: _Tracked, new: _Tracked) -> None:
        actor = HUMAN if old.tier == "long_term" else rng.choice([HUMAN, RECORDER, ENGINE_ACTOR])
        await engine.supplement(new.decision_id, old.decision_id, actor)

    async def do_archive(d: _Tracked) -> None:
        actor = rng.choice([HUMAN, ENGINE_ACTOR])
        await engine.archive([d.decision_id], actor)
        d.pre_archive_status = d.status
        d.status = "archived"

    async def do_reactivate(d: _Tracked) -> None:
        actor = rng.choice([HUMAN, RECORDER, ENGINE_ACTOR])
        await engine.reactivate(d.decision_id, actor)
        assert d.pre_archive_status is not None
        d.status = d.pre_archive_status
        d.pre_archive_status = None

    while acts_run < target_acts or len(pool) < min_pool:
        candidates: list[Callable[[], Awaitable[None]]] = [do_record]
        for d in pool:
            if (
                d.tier == "short_term"
                and d.status in ("current", "not_promoted", "archived")
                and (d.open_promote_item is None)
            ):
                candidates.append(lambda d=d: do_recommend(d))
            if d.open_promote_item is not None:
                candidates.append(lambda d=d: do_promote(d))
                candidates.append(lambda d=d: do_decline(d))
            if d.tier == "short_term" and d.status in ("current", "not_promoted", "archived"):
                candidates.append(lambda d=d: do_discard(d))
            if d.tier == "short_term" and d.status == "archived":
                candidates.append(lambda d=d: do_reactivate(d))
            if (
                d.tier == "short_term"
                and d.status in ("current", "not_promoted")
                and d.open_promote_item is None
            ):
                candidates.append(lambda d=d: do_archive(d))

        # supersede/supplement: newer-supersedes-older (by seq) keeps the graph
        # acyclic by construction; same-tier only (FR-5.2a).
        supersedable = [
            d
            for d in pool
            if (d.tier == "short_term" and d.status in ("current", "not_promoted"))
            or (d.tier == "long_term" and d.status == "current")
        ]
        for old in supersedable:
            newer_same_tier = [
                n for n in pool if n.tier == old.tier and n.seq > old.seq and n is not old
            ]
            if newer_same_tier:
                new = rng.choice(newer_same_tier)
                candidates.append((lambda old=old, new=new: do_supersede(old, new)))

        if len(pool) >= 2:
            for _ in range(min(5, len(pool))):
                old, new = rng.sample(pool, 2)
                candidates.append((lambda old=old, new=new: do_supplement(old, new)))

        eligible_sources = [
            d for d in pool if d.tier == "short_term" and d.status in ("current", "not_promoted")
        ]
        if eligible_sources:
            k = rng.randint(1, min(3, len(eligible_sources)))
            sources = rng.sample(eligible_sources, k)
            candidates.append(lambda sources=sources: do_promote_refined(sources))

        action = rng.choice(candidates)
        await action()
        acts_run += 1

    assert acts_run >= 200
    assert len(pool) >= 30

    for tracked in pool:
        hist = await store.history(tracked.decision_id)
        assert hist.decision.status == tracked.status, (
            f"tracked status {tracked.status!r} != stored status {hist.decision.status!r} "
            f"for {tracked.decision_id}"
        )
        assert _fold(hist.transitions) == tracked.status, (
            f"fold(transitions) != decisions.status for {tracked.decision_id} (I-1 violated)"
        )
        assert hist.decision.scenario == tracked.scenario
        assert hist.decision.domain == "eng"
        assert hist.decision.tier == tracked.tier


# ===========================================================================
# Targeted tests (Step 3)
# ===========================================================================


class TestTargeted:
    async def test_promote_refined_multi_source_consolidation(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        s1 = await engine.record(_nd(scenario="service A retry policy"), RECORDER)
        s2 = await engine.record(_nd(scenario="service B retry policy"), RECORDER)
        refined = await engine.promote_refined(
            [s1.decision_id, s2.decision_id],
            _nd(scenario="general retry policy for all remote calls"),
            HUMAN,
        )
        assert refined.tier == "long_term"
        assert refined.recorded_by == HUMAN

        hist = await store.history(refined.decision_id)
        promoted_from = [link.to_id for link in hist.links if link.kind == "PROMOTED_FROM"]
        assert sorted(promoted_from) == sorted([s1.decision_id, s2.decision_id])

        for source in (s1, s2):
            assert await _status(store, source.decision_id) == "promoted"
            src_hist = await store.history(source.decision_id)
            promoted_transitions = [t for t in src_hist.transitions if t.action == "promoted"]
            assert len(promoted_transitions) == 1
            assert promoted_transitions[0].payload is not None
            assert promoted_transitions[0].payload["refined"] is True

    async def test_pending_lt_claim_executes_at_gate_with_from_lt_copy(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        lt_target = await engine.record_long_term(_nd(scenario="old durable policy"), HUMAN)
        source = await engine.record(
            _nd(
                scenario="agent claims this supersedes durable policy",
                supersedes=[lt_target.decision_id],
            ),
            RECORDER,
        )
        # Declaring supersession of a long-term target files a pending claim,
        # not an immediate link (I-2) — the target is untouched so far.
        assert await _status(store, lt_target.decision_id) == "current"

        item_id = await engine.recommend(source.decision_id, RECORDER, "promote me")
        assert item_id is not None
        lt_copy = await engine.promote(item_id, HUMAN)

        assert await _status(store, lt_target.decision_id) == "superseded"
        target_hist = await store.history(lt_target.decision_id)
        supersedes_links = [
            link
            for link in target_hist.links
            if link.kind == "SUPERSEDES" and link.to_id == lt_target.decision_id
        ]
        assert len(supersedes_links) == 1
        assert supersedes_links[0].from_id == lt_copy.decision_id
        assert supersedes_links[0].from_id != source.decision_id

    async def test_cross_tier_supersede_refused_short_term_old_long_term_new(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        old = await reach(engine, store, "short_term", "current")
        new = await reach(engine, store, "long_term", "current")
        with pytest.raises(InvalidTransition):
            await engine.supersede(new.decision_id, old.decision_id, HUMAN)

    async def test_cross_tier_supersede_refused_long_term_old_short_term_new(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        old = await reach(engine, store, "long_term", "current")
        new = await reach(engine, store, "short_term", "current")
        with pytest.raises(InvalidTransition):
            await engine.supersede(new.decision_id, old.decision_id, HUMAN)

    async def test_cycle_refused(self, engine: LifecycleEngine, store: PostgresStore) -> None:
        a = await engine.record(_nd(scenario="decision A"), RECORDER)
        b = await engine.record(_nd(scenario="decision B"), RECORDER)
        await engine.supersede(a.decision_id, b.decision_id, RECORDER)  # A supersedes B
        with pytest.raises(InvalidTransition):
            await engine.supersede(b.decision_id, a.decision_id, RECORDER)  # B supersedes A

    async def test_promote_vs_supersede_race(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        """Two concurrent acts on the same decision: one must win and commit,
        the other must lose with InvalidTransition (never a partial write, never
        both succeeding) — I-1's row-lock serialization guarantee."""
        d = await engine.record(_nd(scenario="contested decision"), RECORDER)
        item_id = await engine.recommend(d.decision_id, RECORDER, "promote me")
        assert item_id is not None
        successor = await engine.record(_nd(scenario="rival successor"), RECORDER)

        results = await asyncio.gather(
            engine.promote(item_id, HUMAN),
            engine.supersede(successor.decision_id, d.decision_id, RECORDER),
            return_exceptions=True,
        )

        outcomes = [r for r in results if not isinstance(r, BaseException)]
        errors = [r for r in results if isinstance(r, BaseException)]
        assert len(outcomes) == 1, f"expected exactly one success, got {results!r}"
        assert len(errors) == 1, f"expected exactly one failure, got {results!r}"
        assert isinstance(errors[0], InvalidTransition), (
            f"expected InvalidTransition, got {errors[0]!r}"
        )

        hist = await store.history(d.decision_id)
        assert hist.decision.status in ("promoted", "superseded")
        assert _fold(hist.transitions) == hist.decision.status

    async def test_recommend_on_archived_reactivates(
        self, engine: LifecycleEngine, store: PostgresStore
    ) -> None:
        d = await reach(engine, store, "short_term", "not_promoted")
        await engine.archive([d.decision_id], ENGINE_ACTOR)
        assert await _status(store, d.decision_id) == "archived"

        item_id = await engine.recommend(d.decision_id, RECORDER, "worth another look")
        assert item_id is not None
        assert await _status(store, d.decision_id) == "not_promoted"

        hist = await store.history(d.decision_id)
        # The last two transitions are this recommend() call's own pair — the
        # implicit reactivation followed immediately by the recommendation,
        # both in the one transaction (FR-3.4).
        assert [t.action for t in hist.transitions[-2:]] == ["reactivated", "recommended"]
        reactivated = hist.transitions[-2]
        assert reactivated.new_status == "not_promoted"
