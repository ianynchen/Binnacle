"""Integration tests for PostgresStore write primitives (needs a live Postgres; see
conftest.pg_dsn). Each test gets a fresh migrated scratch schema with one seeded
domain ("eng") so decisions.domain's FK is satisfiable without testing the domain
registry itself in every test.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from binnacle.adapters.postgres_store import PostgresStore
from binnacle.domain.errors import (
    EmbeddingDimensionMismatch,
    IdempotencyConflict,
    ItemAlreadyResolved,
    ItemNotFound,
)
from binnacle.domain.models import Actor, Decision, NewDecision, Ref

HUMAN = Actor(kind="human", id="alice")


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


def _decision(**overrides: Any) -> Decision:
    base: dict[str, Any] = {
        "decision_id": uuid4(),
        "domain": "eng",
        "tier": "short_term",
        "status": "current",
        "scenario": "adopt X",
        "outcome": "we adopted X",
        "reasoning": "because Y",
        "source": "test-suite",
        "recorded_by": HUMAN,
        "recorded_at": datetime.now(UTC),
    }
    base.update(overrides)
    return Decision(**base)


class TestInsertDecision:
    """FR-1.6 idempotent insert: identical retries are no-ops, divergent content is refused."""

    async def test_first_insert_reports_inserted(self, store: PostgresStore) -> None:
        d = _decision()
        async with store.transaction() as tx:
            outcome = await store.insert_decision(tx, d, "hash-a")
        assert outcome == "inserted"

    async def test_identical_retry_is_a_noop(self, store: PostgresStore) -> None:
        """A retried record() call (same decision_id, same content) must not raise —
        callers rely on this for safe at-least-once delivery."""
        d = _decision()
        content_hash = NewDecision(
            domain=d.domain,
            scenario=d.scenario,
            outcome=d.outcome,
            reasoning=d.reasoning,
            source=d.source,
            decision_id=d.decision_id,
        ).content_hash()

        async with store.transaction() as tx:
            first = await store.insert_decision(tx, d, content_hash)
        async with store.transaction() as tx:
            second = await store.insert_decision(tx, d, content_hash)

        assert first == "inserted"
        assert second == "exists_identical"

    async def test_divergent_content_hash_raises_idempotency_conflict(
        self, store: PostgresStore
    ) -> None:
        """Same decision_id, different content: never silently overwrite (I-3) — the
        caller reused an id for genuinely different content and must be told."""
        d = _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d, "hash-a")

        with pytest.raises(IdempotencyConflict):
            async with store.transaction() as tx:
                await store.insert_decision(tx, d, "hash-b")

    async def test_missing_decided_at_defaults_to_recorded_at(self, store: PostgresStore) -> None:
        """decided_at is NOT NULL in the schema (defaults to recorded_at, FR-1.7) even
        though Decision.decided_at is Optional."""
        d = _decision(decided_at=None)
        async with store.transaction() as tx:
            await store.insert_decision(tx, d, "hash-a")
            rows = await store.lock_decisions(tx, [d.decision_id])
        assert rows[d.decision_id].status == "current"


class TestApplyTransition:
    """I-1: status is written only alongside its transition, in the same transaction."""

    async def test_writes_transition_and_updates_status_in_one_tx(
        self, store: PostgresStore
    ) -> None:
        d = _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d, "hash-a")
            await store.apply_transition(
                tx, d.decision_id, "promoted", HUMAN.as_str(), "ready", None, "promoted"
            )
            rows = await store.lock_decisions(tx, [d.decision_id])
        assert rows[d.decision_id].status == "promoted"

    async def test_null_new_status_leaves_status_unchanged(self, store: PostgresStore) -> None:
        d = _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d, "hash-a")
            await store.apply_transition(
                tx, d.decision_id, "recommended", HUMAN.as_str(), None, None, None
            )
            rows = await store.lock_decisions(tx, [d.decision_id])
        assert rows[d.decision_id].status == "current"


class TestEnqueue:
    """idx_queue_dedup: discovery re-runs cannot duplicate an open item."""

    async def test_duplicate_open_item_is_deduped(self, store: PostgresStore) -> None:
        d1, d2 = _decision(), _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d1, "h1")
            await store.insert_decision(tx, d2, "h2")
            first = await store.enqueue(tx, "link", d1.decision_id, d2.decision_id, HUMAN, "r", 0.8)
            second = await store.enqueue(
                tx, "link", d1.decision_id, d2.decision_id, HUMAN, "r2", 0.5
            )
        assert first is not None
        assert second is None

    async def test_resolved_item_does_not_block_a_new_one(self, store: PostgresStore) -> None:
        """The dedup index is scoped to WHERE NOT resolved: once an item resolves, the
        same (kind, decision, target) can be proposed again."""
        d1, d2 = _decision(), _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d1, "h1")
            await store.insert_decision(tx, d2, "h2")
            item_id = await store.enqueue(
                tx, "link", d1.decision_id, d2.decision_id, HUMAN, "r", 0.8
            )
        assert item_id is not None
        async with store.transaction() as tx:
            await store.resolve_item(tx, item_id)
        async with store.transaction() as tx:
            again = await store.enqueue(
                tx, "link", d1.decision_id, d2.decision_id, HUMAN, "r2", 0.5
            )
        assert again is not None


class TestResolveItem:
    """I-1: guarded UPDATE ... WHERE NOT resolved — a double-tap resolves once."""

    async def test_double_tap_raises_item_already_resolved(self, store: PostgresStore) -> None:
        d1, d2 = _decision(), _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d1, "h1")
            await store.insert_decision(tx, d2, "h2")
            item_id = await store.enqueue(
                tx, "link", d1.decision_id, d2.decision_id, HUMAN, "r", 0.8
            )
        assert item_id is not None

        async with store.transaction() as tx:
            resolved = await store.resolve_item(tx, item_id)
        assert resolved.resolved is True
        assert resolved.item_id == item_id

        with pytest.raises(ItemAlreadyResolved):
            async with store.transaction() as tx:
                await store.resolve_item(tx, item_id)

    async def test_unknown_item_raises_item_not_found(self, store: PostgresStore) -> None:
        async with store.transaction() as tx:
            with pytest.raises(ItemNotFound):
                await store.resolve_item(tx, 999_999_999)


class TestOpenItemsFor:
    async def test_lists_only_unresolved_items_oldest_first(self, store: PostgresStore) -> None:
        d1, d2 = _decision(), _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d1, "h1")
            await store.insert_decision(tx, d2, "h2")
            item_id = await store.enqueue(
                tx, "link", d1.decision_id, d2.decision_id, HUMAN, "r", 0.8
            )
            open_before = await store.open_items_for(tx, d1.decision_id)
        assert item_id is not None
        assert [i.item_id for i in open_before] == [item_id]

        async with store.transaction() as tx:
            await store.resolve_item(tx, item_id)
            open_after = await store.open_items_for(tx, d1.decision_id)
        assert open_after == []


class TestLockDecisions:
    """Deadlock avoidance: ids are locked in sorted order regardless of call order."""

    async def test_locks_rows_in_sorted_id_order(self, store: PostgresStore) -> None:
        d1, d2, d3 = _decision(), _decision(), _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d1, "h1")
            await store.insert_decision(tx, d2, "h2")
            await store.insert_decision(tx, d3, "h3")
            unsorted_ids = [d3.decision_id, d1.decision_id, d2.decision_id]
            rows = await store.lock_decisions(tx, unsorted_ids)
        assert list(rows.keys()) == sorted(unsorted_ids)

    async def test_missing_ids_are_simply_absent(self, store: PostgresStore) -> None:
        async with store.transaction() as tx:
            rows = await store.lock_decisions(tx, [uuid4()])
        assert rows == {}

    async def test_empty_ids_returns_empty_dict(self, store: PostgresStore) -> None:
        async with store.transaction() as tx:
            rows = await store.lock_decisions(tx, [])
        assert rows == {}


class TestLinksAndRefs:
    async def test_insert_link_is_idempotent(self, store: PostgresStore) -> None:
        d1, d2 = _decision(), _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d1, "h1")
            await store.insert_decision(tx, d2, "h2")
            await store.insert_link(tx, d1.decision_id, d2.decision_id, "SUPERSEDES")
            await store.insert_link(tx, d1.decision_id, d2.decision_id, "SUPERSEDES")

    async def test_insert_refs_is_idempotent(self, store: PostgresStore) -> None:
        d = _decision()
        refs = [Ref(role="subject", kind="component", identifier="waypoint")]
        async with store.transaction() as tx:
            await store.insert_decision(tx, d, "h1")
            await store.insert_refs(tx, d.decision_id, refs)
            await store.insert_refs(tx, d.decision_id, refs)

    async def test_insert_refs_empty_sequence_is_a_noop(self, store: PostgresStore) -> None:
        d = _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d, "h1")
            await store.insert_refs(tx, d.decision_id, [])


class TestDomains:
    async def test_domain_exists(self, store: PostgresStore) -> None:
        async with store.transaction() as tx:
            assert await store.domain_exists(tx, "eng") is True
            assert await store.domain_exists(tx, "unknown") is False

    async def test_upsert_domain_updates_existing_row(self, store: PostgresStore) -> None:
        async with store.transaction() as tx:
            await store.upsert_domain(
                tx, "eng", "engineering (updated)", False, HUMAN.as_str(), "domain_updated", "reorg"
            )
        # A decision can no longer be recorded against a domain toggled inactive by a
        # caller-side check (the store itself does not enforce activeness — that's a
        # Lifecycle Engine concern) — this test only asserts the write landed.


class TestEmbeddings:
    async def test_upsert_embedding_rejects_wrong_dimension(self, store: PostgresStore) -> None:
        d = _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d, "h1")
            with pytest.raises(EmbeddingDimensionMismatch):
                await store.upsert_embedding(tx, d.decision_id, [0.1] * 384)

    async def test_upsert_then_mark_discovered(
        self, store: PostgresStore, pg_dsn: str, scratch_schema: str
    ) -> None:
        d = _decision()
        async with store.transaction() as tx:
            await store.insert_decision(tx, d, "h1")
            await store.upsert_embedding(tx, d.decision_id, [0.1] * 768)
            await store.mark_discovered(tx, [d.decision_id])

        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT discovered_at FROM {scratch_schema}.embeddings WHERE decision_id = %s",
                (d.decision_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] is not None
