"""Integration tests for the three sweeps (docs/components/04-query-and-assist.md
"The sweeps"; REQUIREMENTS FR-6.9/FR-7.4/FR-3.4; needs a live Postgres, see
conftest.pg_dsn): `application.discovery.backfill_embeddings`, `.discover`, and
`application.archival.archive_stale`, exercised directly against `PostgresStore`
+ `StubEmbedder`/`ScriptedSuggester`, plus a thin `TestClientWiring` check that
`Binnacle`'s three delegating methods actually reach them.

Layout:
- TestBackfillEmbeddings: drains the unembedded backlog, no-ops a second time,
  aborts (backlog intact) on a dimension mismatch.
- TestDiscoverNoAllPairs: the FR-7.4 mechanical bound -- total pairs handed to
  `Suggester.classify_pairs` across a whole sweep is <= N*k, never the
  quadratic all-pairs count.
- TestDiscoverCursor: process-death-and-resume (kill between classify and
  mark), and dedup tolerance on a forced rerun.
- TestDiscoverCap: `per_sweep_cap` stops enqueueing mid-sweep and leaves the
  unfinished decision's `discovered_at` NULL for the next sweep.
- TestDiscoverNoSuggester: a `None` suggester no-ops the whole sweep, cursor
  untouched.
- TestPromotionAssessment: `discover()`'s other half -- `assess_promotion`
  over aging unrecommended decisions, routed through `LifecycleEngine.recommend`.
- TestArchiveStale: only clock-eligible decisions move, an open queue item
  blocks the clock, the actor recorded is `engine:binnacle`.
- TestClientWiring: `Binnacle.backfill_embeddings/discover/archive_stale`
  delegate to the modules above.
"""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from binnacle.adapters.postgres_store import PostgresStore
from binnacle.application.archival import archive_stale
from binnacle.application.config import BinnacleConfig
from binnacle.application.discovery import backfill_embeddings, discover
from binnacle.application.lifecycle import LifecycleEngine
from binnacle.application.ports import Tx
from binnacle.client import Binnacle
from binnacle.domain.errors import EmbeddingDimensionMismatch
from binnacle.domain.models import Actor, Decision, PromotionAssessment, Suggestion
from tests.helpers import ScriptedSuggester, StubEmbedder

HUMAN = Actor("human", "alice")
AGENT = Actor("agent", "meridian/s1")
ENGINE_ACTOR = Actor("engine", "binnacle")

DIM = 16


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


async def _seed(store: PostgresStore, decision: Decision) -> UUID:
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
    return decision.decision_id


@pytest.fixture()
async def store(pg_dsn: str, scratch_schema: str) -> AsyncIterator[PostgresStore]:
    s = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=DIM)
    await s.migrate()
    async with s.transaction() as tx:
        await s.upsert_domain(
            tx, "eng", "engineering decisions", True, HUMAN.as_str(), "domain_created", None
        )
    yield s
    await s.aclose()


@pytest.fixture()
def embedder() -> StubEmbedder:
    return StubEmbedder(dim=DIM)


@pytest.fixture()
def engine(store: PostgresStore) -> LifecycleEngine:
    return LifecycleEngine(store)


async def _reset_discovered_at(pg_dsn: str, schema: str, decision_id: UUID) -> None:
    """Test-only poke: force a decision back onto the discovery cursor,
    simulating whatever operational reason a decision might need
    reprocessing, so the enqueue-dedup path can be exercised on a rerun."""
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            f'UPDATE "{schema}".embeddings SET discovered_at = NULL WHERE decision_id = %s',
            (decision_id,),
        )


class TestBackfillEmbeddings:
    """FR-6.9: the unembedded backlog -> `Embedder.embed` -> `upsert_embedding`."""

    async def test_drains_backlog(self, store: PostgresStore, embedder: StubEmbedder) -> None:
        for _ in range(3):
            await _seed(store, _decision())

        summary = await backfill_embeddings(store, embedder, DIM, batch=100)

        assert summary.embedded == 3
        assert await store.unembedded(100) == []

    async def test_second_run_is_a_clean_noop(
        self, store: PostgresStore, embedder: StubEmbedder
    ) -> None:
        await _seed(store, _decision())
        first = await backfill_embeddings(store, embedder, DIM, batch=100)
        assert first.embedded == 1

        second = await backfill_embeddings(store, embedder, DIM, batch=100)

        assert second.embedded == 0

    async def test_empty_backlog_noops(self, store: PostgresStore, embedder: StubEmbedder) -> None:
        summary = await backfill_embeddings(store, embedder, DIM, batch=100)
        assert summary.embedded == 0

    async def test_wrong_dimension_aborts_with_backlog_intact(self, store: PostgresStore) -> None:
        """`embedding_dim` (from config) disagrees with what the embedder
        actually returns -- a config bug, not a data bug. Nothing gets
        upserted, so the backlog is untouched (I-5) and callers see a typed
        error, not silent data loss."""
        wrong_dim_embedder = StubEmbedder(dim=DIM + 1)
        d1 = await _seed(store, _decision())
        d2 = await _seed(store, _decision())

        with pytest.raises(EmbeddingDimensionMismatch):
            await backfill_embeddings(store, wrong_dim_embedder, DIM, batch=100)

        remaining = {d.decision_id for d in await store.unembedded(100)}
        assert remaining == {d1, d2}


class TestDiscoverNoAllPairs:
    """docs/components/04 Contract points: "Discovery work is O(k) per new
    decision... MUST NOT contain any all-pairs path" -- the mechanical bound
    the contract demands an explicit test for."""

    async def test_suggester_call_count_bounded_by_n_times_k(
        self, store: PostgresStore, embedder: StubEmbedder, engine: LifecycleEngine
    ) -> None:
        n, k = 15, 3
        now = datetime.now(UTC)
        for i in range(n):
            await _seed(store, _decision(recorded_at=now - timedelta(days=n - i)))
        await backfill_embeddings(store, embedder, DIM, batch=100)

        # Generous script -- this test asserts an upper bound on demand, not
        # an exact count, so over-provision rather than compute it exactly.
        suggester = ScriptedSuggester(
            pair_suggestions=[
                Suggestion(kind="unrelated", rationale="unrelated", confidence=0.9)
                for _ in range(n * k)
            ]
        )

        await discover(
            store,
            embedder,
            suggester,
            engine,
            k=k,
            confidence_floor=0.0,
            per_sweep_cap=10_000,
            archival_age_days=90,
            batch=100,
        )

        total_pairs = sum(len(call) for call in suggester.classify_pairs_calls)
        all_pairs_count = n * (n - 1) // 2
        assert total_pairs <= n * k
        # A regression to an all-pairs scan would blow well past the bound at
        # this N/k (105 vs 45) -- assert it explicitly, not just implicitly.
        assert total_pairs < all_pairs_count


class TestDiscoverCursor:
    """FR-7.4's discovery cursor: `discovered_at` marks a fully-processed
    decision, only inside the same transaction as its own enqueues, so a
    death between classifying and marking resumes cleanly."""

    async def _seed_pair(self, store: PostgresStore, embedder: StubEmbedder) -> tuple[UUID, UUID]:
        now = datetime.now(UTC)
        old = await _seed(store, _decision(recorded_at=now - timedelta(days=2)))
        new = await _seed(store, _decision(recorded_at=now - timedelta(days=1)))
        await backfill_embeddings(store, embedder, DIM, batch=100)
        return old, new

    async def test_resumes_after_death_between_classify_and_mark(
        self,
        store: PostgresStore,
        embedder: StubEmbedder,
        engine: LifecycleEngine,
        pg_dsn: str,
        scratch_schema: str,
    ) -> None:
        _old, new = await self._seed_pair(store, embedder)

        # Drain `old` from the cursor cleanly first (0 candidates -- it has
        # nothing recorded before it, so `classify_pairs` is never even
        # called for it) so the dying run below lands exactly on `new`'s own
        # mark_discovered, not on an incidental earlier decision's.
        pre = await discover(
            store,
            embedder,
            ScriptedSuggester(),
            engine,
            k=5,
            confidence_floor=0.0,
            per_sweep_cap=100,
            archival_age_days=90,
            batch=1,
        )
        assert pre.decisions_processed == 1
        assert {i for i in await store.undiscovered(100)} == {new}

        class _DiesOnMark(PostgresStore):
            async def mark_discovered(self, tx: Tx, decision_ids: Sequence[UUID]) -> None:
                msg = "simulated process death"
                raise RuntimeError(msg)

        dying_store = _DiesOnMark(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=DIM)
        dying_engine = LifecycleEngine(dying_store)
        dying_suggester = ScriptedSuggester(
            pair_suggestions=[Suggestion(kind="supersedes", rationale="r", confidence=0.9)]
        )
        try:
            with pytest.raises(RuntimeError, match="simulated process death"):
                await discover(
                    dying_store,
                    embedder,
                    dying_suggester,
                    dying_engine,
                    k=5,
                    confidence_floor=0.0,
                    per_sweep_cap=100,
                    archival_age_days=90,
                    batch=100,
                )
        finally:
            await dying_store.aclose()

        # The whole transaction (enqueue + mark_discovered) rolled back: the
        # cursor didn't move, and the partial enqueue didn't stick either.
        assert new in {i for i in await store.undiscovered(100)}
        assert await store.open_queue(kinds=["supersede"]) == []

        # A fresh sweep (a fresh Suggester call -- the crashed one is done)
        # resumes from exactly where the dead one left off.
        resumed_suggester = ScriptedSuggester(
            pair_suggestions=[Suggestion(kind="supersedes", rationale="r", confidence=0.9)]
        )
        summary = await discover(
            store,
            embedder,
            resumed_suggester,
            engine,
            k=5,
            confidence_floor=0.0,
            per_sweep_cap=100,
            archival_age_days=90,
            batch=100,
        )

        assert summary.suggestions_enqueued == 1
        assert await store.undiscovered(100) == []

    async def test_dedup_on_forced_rerun_is_tolerated_and_counted(
        self,
        store: PostgresStore,
        embedder: StubEmbedder,
        engine: LifecycleEngine,
        pg_dsn: str,
        scratch_schema: str,
    ) -> None:
        _old, new = await self._seed_pair(store, embedder)
        first_suggester = ScriptedSuggester(
            pair_suggestions=[Suggestion(kind="supersedes", rationale="r", confidence=0.9)]
        )
        first = await discover(
            store,
            embedder,
            first_suggester,
            engine,
            k=5,
            confidence_floor=0.0,
            per_sweep_cap=100,
            archival_age_days=90,
            batch=100,
        )
        assert first.suggestions_enqueued == 1

        await _reset_discovered_at(pg_dsn, scratch_schema, new)
        second_suggester = ScriptedSuggester(
            pair_suggestions=[Suggestion(kind="supersedes", rationale="r", confidence=0.9)]
        )
        second = await discover(
            store,
            embedder,
            second_suggester,
            engine,
            k=5,
            confidence_floor=0.0,
            per_sweep_cap=100,
            archival_age_days=90,
            batch=100,
        )

        assert second.suggestions_enqueued == 0
        assert second.suggestions_deduped == 1
        assert await store.undiscovered(100) == []
        open_items = await store.open_queue(kinds=["supersede"])
        assert len([i for i in open_items if i.item.decision_id == new]) == 1


class TestDiscoverCap:
    """`per_sweep_cap` bounds total enqueues for the whole sweep call: once
    hit, no further decision is even started, and the decision mid-cap is
    left un-marked for the next sweep."""

    async def test_cap_overflow_leaves_discovered_at_null(
        self, store: PostgresStore, embedder: StubEmbedder, engine: LifecycleEngine
    ) -> None:
        now = datetime.now(UTC)
        d0 = await _seed(store, _decision(recorded_at=now - timedelta(days=3)))
        d1 = await _seed(store, _decision(recorded_at=now - timedelta(days=2)))
        d2 = await _seed(store, _decision(recorded_at=now - timedelta(days=1)))
        # One backfill call per decision: distinct `embedded_at` timestamps,
        # so `undiscovered()`'s embedded_at-ASC cursor gives a deterministic
        # d0, d1, d2 processing order.
        for _ in range(3):
            await backfill_embeddings(store, embedder, DIM, batch=1)

        # k covers every other seeded decision, so temporal order alone
        # decides survivors: d0 sees none, d1 sees {d0}, d2 sees {d0, d1}.
        suggester = ScriptedSuggester(
            pair_suggestions=[
                Suggestion(kind="supersedes", rationale="r", confidence=0.9) for _ in range(3)
            ]
        )

        summary = await discover(
            store,
            embedder,
            suggester,
            engine,
            k=2,
            confidence_floor=0.0,
            per_sweep_cap=2,
            archival_age_days=90,
            batch=100,
        )

        assert summary.suggestions_enqueued == 2
        assert summary.decisions_processed == 2  # d0 (0 pairs), d1 (1 pair) -- d2 cut off

        remaining = {i for i in await store.undiscovered(100)}
        assert remaining == {d2}
        assert d0 not in remaining
        assert d1 not in remaining


class TestDiscoverNoSuggester:
    async def test_no_suggester_configured_noops_cleanly(
        self, store: PostgresStore, embedder: StubEmbedder, engine: LifecycleEngine
    ) -> None:
        await _seed(store, _decision())
        await backfill_embeddings(store, embedder, DIM, batch=100)
        before = {i for i in await store.undiscovered(100)}
        assert before  # sanity: there IS a cursor to leave untouched

        summary = await discover(
            store,
            embedder,
            None,
            engine,
            k=5,
            confidence_floor=0.6,
            per_sweep_cap=50,
            archival_age_days=90,
            batch=100,
        )

        assert summary.decisions_processed == 0
        assert summary.suggestions_enqueued == 0
        assert summary.suggestions_deduped == 0
        assert summary.suggestions_below_floor == 0
        assert summary.promotions_recommended == 0
        assert {i for i in await store.undiscovered(100)} == before


class TestPromotionAssessment:
    """discover()'s other half (FR-7.2): `assess_promotion` over aging
    unrecommended decisions, routed through `LifecycleEngine.recommend` (the
    audited path) rather than a direct `enqueue`."""

    async def _seed_aging(self, store: PostgresStore) -> UUID:
        now = datetime.now(UTC)
        return await _seed(store, _decision(recorded_at=now - timedelta(days=60)))

    async def test_positive_assessment_recommends_via_engine(
        self, store: PostgresStore, embedder: StubEmbedder, engine: LifecycleEngine
    ) -> None:
        aging_id = await self._seed_aging(store)
        suggester = ScriptedSuggester(
            promotion_assessments=[
                PromotionAssessment(
                    decision_id=aging_id, recommend=True, rationale="stable", confidence=0.9
                )
            ]
        )

        summary = await discover(
            store,
            embedder,
            suggester,
            engine,
            k=5,
            confidence_floor=0.6,
            per_sweep_cap=50,
            archival_age_days=90,
            batch=100,
        )

        assert summary.promotions_recommended == 1
        open_items = await store.open_queue(kinds=["promote"])
        matching = [i for i in open_items if i.item.decision_id == aging_id]
        assert len(matching) == 1
        assert matching[0].item.proposed_by == ENGINE_ACTOR

        history = await store.history(aging_id)
        recommended = [t for t in history.transitions if t.action == "recommended"]
        assert len(recommended) == 1
        assert recommended[0].actor == ENGINE_ACTOR

    async def test_negative_assessment_does_not_recommend(
        self, store: PostgresStore, embedder: StubEmbedder, engine: LifecycleEngine
    ) -> None:
        aging_id = await self._seed_aging(store)
        suggester = ScriptedSuggester(
            promotion_assessments=[
                PromotionAssessment(
                    decision_id=aging_id, recommend=False, rationale="too soon", confidence=0.9
                )
            ]
        )

        summary = await discover(
            store,
            embedder,
            suggester,
            engine,
            k=5,
            confidence_floor=0.6,
            per_sweep_cap=50,
            archival_age_days=90,
            batch=100,
        )

        assert summary.promotions_recommended == 0
        assert await store.open_queue(kinds=["promote"]) == []


class TestArchiveStale:
    """FR-3.4: the clock-driven auto-archival sweep."""

    async def test_only_clock_eligible_decisions_are_archived(
        self, store: PostgresStore, engine: LifecycleEngine
    ) -> None:
        now = datetime.now(UTC)
        stale = await _seed(store, _decision(recorded_at=now - timedelta(days=100)))
        fresh = await _seed(store, _decision(recorded_at=now - timedelta(days=1)))

        summary = await archive_stale(store, engine, archival_age_days=90)

        assert summary.archived == 1
        stale_history = await store.history(stale)
        assert stale_history.decision.status == "archived"
        fresh_history = await store.history(fresh)
        assert fresh_history.decision.status == "current"

    async def test_open_queue_item_blocks_archival(
        self, store: PostgresStore, engine: LifecycleEngine
    ) -> None:
        now = datetime.now(UTC)
        blocked = await _seed(store, _decision(recorded_at=now - timedelta(days=100)))
        await engine.recommend(blocked, AGENT, "please review")

        summary = await archive_stale(store, engine, archival_age_days=90)

        assert summary.archived == 0
        history = await store.history(blocked)
        assert history.decision.status == "current"

    async def test_engine_actor_recorded_on_the_archived_transition(
        self, store: PostgresStore, engine: LifecycleEngine
    ) -> None:
        now = datetime.now(UTC)
        stale = await _seed(store, _decision(recorded_at=now - timedelta(days=100)))

        await archive_stale(store, engine, archival_age_days=90)

        history = await store.history(stale)
        archived = [t for t in history.transitions if t.action == "archived"]
        assert len(archived) == 1
        assert archived[0].actor == ENGINE_ACTOR

    async def test_empty_input_noops(self, store: PostgresStore, engine: LifecycleEngine) -> None:
        summary = await archive_stale(store, engine, archival_age_days=90)
        assert summary.archived == 0


class TestClientWiring:
    """Thin end-to-end check that `Binnacle`'s three sweep methods delegate
    to `application.discovery`/`application.archival` -- the real coverage
    lives in the classes above."""

    async def test_backfill_discover_archive_delegate(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        suggester = ScriptedSuggester(
            pair_suggestions=[Suggestion(kind="supersedes", rationale="r", confidence=0.9)]
        )
        config = BinnacleConfig(
            dsn=pg_dsn,
            schema_name=scratch_schema,
            embedder=StubEmbedder(dim=DIM),
            embedding_dim=DIM,
            suggester=suggester,
            archival_age_days=90,
        )
        client = Binnacle(config)
        await client.migrate()
        await client.add_domain("eng", "engineering", actor=HUMAN)

        raw_store = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=DIM)
        try:
            now = datetime.now(UTC)
            old = await _seed(raw_store, _decision(recorded_at=now - timedelta(days=2)))
            new = await _seed(raw_store, _decision(recorded_at=now - timedelta(days=1)))
            stale = await _seed(raw_store, _decision(recorded_at=now - timedelta(days=100)))

            backfill_summary = await client.backfill_embeddings(batch=100)
            assert backfill_summary.embedded == 3

            # archive_stale() first: `stale` is the only clock-eligible
            # decision and carries no queue items yet, so it archives clean
            # -- ordered before discover() so the sweeps stay independent
            # here (an open discovery suggestion targeting `stale` would
            # otherwise block its own archival clock, correctly, but that's
            # not what this delegation smoke test is checking).
            archive_summary = await client.archive_stale()
            assert archive_summary.archived == 1
            stale_history = await client.history(stale)
            assert stale_history.decision.status == "archived"

            discover_summary = await client.discover(batch=100)
            assert discover_summary.suggestions_enqueued == 1

            open_items = await client.queue(kinds=["supersede"])
            assert any(v.item.decision_id == new and v.item.target_id == old for v in open_items)
        finally:
            await raw_store.aclose()
            await client.aclose()
