"""Store port: the transaction handle and write-primitive contract adapters implement.

`domain` imports no DB driver; this module stays driver-free too (import-linter's
"application is driver-free" contract) — `Tx` is an opaque marker the postgres
adapter subclasses to smuggle its real connection through, so callers here never
see psycopg types.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from binnacle.domain.models import (
    Actor,
    Decision,
    LinkKind,
    QueueItem,
    QueueKind,
    Ref,
    Tier,
)

InsertOutcome = Literal["inserted", "exists_identical"]


class Tx:
    """Opaque handle for one write transaction, threaded through every store primitive.

    Carries no state itself — adapters attach their own driver-specific connection
    privately (see `adapters.postgres_store._PgTx`), keeping this module free of any
    database driver import.
    """

    __slots__ = ()


@dataclass(frozen=True)
class DecisionRow:
    """The subset of a `decisions` row a lifecycle act needs after locking it.

    Deliberately narrower than `Decision`: `lock_decisions` exists to acquire
    `SELECT ... FOR UPDATE` locks and give the caller just enough to validate the
    next transition (I-1/I-2) — status, tier, domain — without paying to hydrate
    full content (options_considered, refs, metadata) on every lifecycle act.
    """

    decision_id: UUID
    tier: Tier
    domain: str
    status: str


class StorePort(Protocol):
    """Transaction + write primitives the Lifecycle Engine composes.

    Every mutation takes an explicit `tx` acquired from `transaction()`; the store
    never commits on its own inside a primitive (ARCHITECTURE.md §4, DR-4 — one
    transaction per lifecycle act is the caller's responsibility, not the store's).
    """

    async def migrate(self) -> None:
        """Apply pending schema migrations, then verify the migrated VECTOR(n)
        dimension matches the configured `embedding_dim`.

        Raises:
            EmbeddingDimensionMismatch: the migrated `embeddings.embedding` column's
                dimension does not equal the configured `embedding_dim`.
        """
        ...

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        """Open one write transaction. Use as `async with store.transaction() as tx:`."""
        ...

    async def lock_decisions(self, tx: Tx, ids: Sequence[UUID]) -> dict[UUID, DecisionRow]:
        """`SELECT ... FOR UPDATE` every row in `ids`, sorted first (deadlock avoidance).

        Ids with no matching row are simply absent from the result — callers that
        need every id to exist check completeness themselves.
        """
        ...

    async def insert_decision(self, tx: Tx, d: Decision, content_hash: str) -> InsertOutcome:
        """Insert `d`, never UPDATE (I-3).

        Returns:
            'inserted' on a fresh row; 'exists_identical' when `d.decision_id`
            already exists with the same `content_hash`.

        Raises:
            IdempotencyConflict: `d.decision_id` exists with a different `content_hash`.
        """
        ...

    async def apply_transition(
        self,
        tx: Tx,
        decision_id: UUID,
        action: str,
        actor: str,
        reason: str | None,
        payload: dict[str, Any] | None,
        new_status: str | None,
    ) -> None:
        """Append one transition row and, when `new_status` is given, update
        `decisions.status` to match — both in `tx` (I-1: status never diverges
        from the transition fold)."""
        ...

    async def insert_link(self, tx: Tx, from_id: UUID, to_id: UUID, kind: LinkKind) -> None:
        """Insert one `links` row. Idempotent: repeating an existing (from, kind, to) is a no-op."""
        ...

    async def insert_refs(self, tx: Tx, decision_id: UUID, refs: Sequence[Ref]) -> None:
        """Insert `refs` rows for `decision_id`. Idempotent per (role, kind, identifier)."""
        ...

    async def enqueue(
        self,
        tx: Tx,
        kind: QueueKind,
        decision_id: UUID,
        target_id: UUID | None,
        proposed_by: Actor,
        rationale: str | None,
        confidence: float | None,
    ) -> int | None:
        """Insert one open queue item.

        Returns:
            The new `item_id`, or `None` when `idx_queue_dedup` already has an open
            item for this (kind, decision_id, target_id) — discovery re-runs cannot
            duplicate open items.
        """
        ...

    async def resolve_item(self, tx: Tx, item_id: int) -> QueueItem:
        """Guarded `UPDATE ... SET resolved = TRUE WHERE item_id = $1 AND NOT resolved`.

        Raises:
            ItemNotFound: no queue row has this `item_id`.
            ItemAlreadyResolved: the row exists but was already resolved (double-tap).
        """
        ...

    async def open_items_for(self, tx: Tx, decision_id: UUID) -> list[QueueItem]:
        """Every unresolved queue item for `decision_id`, oldest first."""
        ...

    async def domain_exists(self, conn_or_tx: Tx, name: str) -> bool:
        """Whether `name` is a registered domain (FR-2.1)."""
        ...

    async def upsert_domain(
        self,
        tx: Tx,
        name: str,
        description: str,
        active: bool,
        actor: str,
        action: str,
        reason: str | None,
    ) -> None:
        """Insert-or-update one `domains` row and append its `domain_transitions` audit row (FR-2.2)."""
        ...

    async def upsert_embedding(self, tx: Tx, decision_id: UUID, vector: list[float]) -> None:
        """Insert-or-update the embedding for `decision_id`.

        Raises:
            EmbeddingDimensionMismatch: `len(vector) != embedding_dim`.
        """
        ...

    async def mark_discovered(self, tx: Tx, decision_ids: Sequence[UUID]) -> None:
        """Set `embeddings.discovered_at = now()` for each id (FR-7.4 discovery cursor)."""
        ...
