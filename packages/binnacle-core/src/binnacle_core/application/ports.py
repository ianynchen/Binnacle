"""Store port: the transaction handle and write-primitive contract adapters implement.

`domain` imports no DB driver; this module stays driver-free too (import-linter's
"application is driver-free" contract) — `Tx` is an opaque marker the postgres
adapter subclasses to smuggle its real connection through, so callers here never
see psycopg types.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, overload, runtime_checkable
from uuid import UUID

from binnacle_core.domain.models import (
    Actor,
    CandidatePair,
    CompactDecision,
    Decision,
    DomainRecord,
    ExportBundle,
    HistoryRecord,
    LinkKind,
    PromotionAssessment,
    QueueItem,
    QueueItemView,
    QueueKind,
    Ref,
    Suggestion,
    Tier,
    Transition,
)

InsertOutcome = Literal["inserted", "exists_identical"]


@runtime_checkable
class Suggester(Protocol):
    """ARCHITECTURE §3.1: the LLM-backed classification port (FR-7.1) — binnacle
    core never constructs an LLM client itself, only calls through this port.
    Meridian fulfills it via tradewind's light tier; tests use a scripted stub.

    `@runtime_checkable` so `BinnacleConfig` (pydantic, `arbitrary_types_allowed`)
    can validate a supplied port with `isinstance` — structural (method names
    only), not signature-checked, same limitation `runtime_checkable` always has.
    """

    async def classify_pairs(self, pairs: list[CandidatePair]) -> list[Suggestion]:
        """Classify each candidate pair as `supersedes` / `supplements` /
        `conflicts` / `unrelated`, with a rationale and confidence (FR-7.2/7.4)."""
        ...

    async def assess_promotion(self, decisions: list[CompactDecision]) -> list[PromotionAssessment]:
        """Assess whether each aging short-term decision is ready to recommend
        for promotion (FR-7.2's promotion-candidate sweep)."""
        ...


@runtime_checkable
class Embedder(Protocol):
    """ARCHITECTURE §3.1: the embedding port (FR-7.1). Meridian fulfills it via
    nomic-embed-text-v1.5 (OQ-3); tests use a deterministic stub.

    `@runtime_checkable` for the same pydantic-validation reason as `Suggester`.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed each of `texts`, preserving order and length."""
        ...


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

    async def predecessor_chain(self, tx: Tx, decision_id: UUID) -> list[UUID]:
        """Every decision `decision_id` itself (transitively) supersedes, nearest
        first: a recursive walk over `links` kind SUPERSEDES starting from
        `from_id = decision_id`, the same shape as `history()`'s predecessor
        chain but executed on `tx`'s own connection.

        Exists so the Lifecycle Engine's acyclicity check (before linking a new
        SUPERSEDES edge) can run INSIDE the act's already-open transaction
        instead of borrowing a second pooled connection via `history()` — under
        concurrent acts, a second connection-per-call exhausts a small pool
        (I-1's serialization is supposed to come from row locks, not from
        starving the pool)."""
        ...

    async def get_decision_tx(self, tx: Tx, decision_id: UUID) -> Decision | None:
        """`get_decision`, but executed on `tx`'s own connection instead of a
        second pooled one — for lifecycle acts that need a decision's full
        content while already holding its row lock (e.g. `promote` copying its
        source's content into the long-term row) inside an open transaction."""
        ...

    async def transitions_for(self, tx: Tx, decision_id: UUID) -> list[Transition]:
        """Every transition for `decision_id`, oldest first — the same query
        `history()` runs, but on `tx`'s own connection. Exists for the same
        pool-exhaustion reason as `predecessor_chain`: `reactivate`/
        `recommend`'s implicit-reactivation path needs a decision's own
        transition log to compute the status to restore, while already holding
        that decision's row lock inside its own open transaction."""
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

    async def domain_active(self, conn_or_tx: Tx, name: str) -> bool | None:
        """`name`'s registry status: `None` when `name` is not registered at
        all, else its `active` flag (FR-2.1/2.2) — lets a caller distinguish
        "unregistered" from "registered but deactivated" in one lookup."""
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

    # -- reads -----------------------------------------------------------------
    # Read-only: no `tx` required, a plain pooled connection suffices (no lock
    # needed, nothing here mutates). See docs/binnacle-core/components/02-store-and-migrations.md
    # ("Reads") and docs/binnacle-core/components/04-query-and-assist.md ("Query contracts").

    async def list_domains(self) -> list[DomainRecord]:
        """FR-2: every registered domain, name-ordered — the read side of
        `upsert_domain`, the same projection `export_rows` already bundles into
        `ExportBundle.domains`, exposed standalone for the client's registry
        query (`Binnacle.domains()`)."""
        ...

    async def get_decision(self, decision_id: UUID) -> Decision | None:
        """Fetch one decision, hydrated with its refs and declared
        supersedes/supplements (FR-6.8). `None` when `decision_id` doesn't exist."""
        ...

    async def get_many(self, ids: Sequence[UUID]) -> list[Decision]:
        """Batch `get_decision` (FR-6.8). Ids with no matching row are simply absent."""
        ...

    async def get_many_compact(
        self, ids: Sequence[UUID], *, compact_chars: int = 200
    ) -> list[CompactDecision]:
        """`get_many`'s compact projection (docs/binnacle-core/components/04's "Compact
        projections are SQL-level" contract point): the same id-list lookup,
        but selecting only the compact columns with `outcome` truncated to
        `compact_chars` in SQL — no full-row fetch then trim. Exists for
        `precedent()`, which hydrates a `knn`-picked id list rather than
        scanning a filtered range. Ids with no matching row are simply absent;
        result order is unspecified (callers that care, like `precedent()`,
        re-sort themselves)."""
        ...

    @overload
    async def relevant(
        self,
        *,
        domains: Sequence[str] | None = None,
        status: Sequence[str] | None = None,
        tier: Tier | None = None,
        subject: tuple[str, str] | None = None,
        as_of: datetime | None = None,
        text: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
        compact_chars: int = 200,
    ) -> list[CompactDecision]: ...

    @overload
    async def relevant(
        self,
        *,
        domains: Sequence[str] | None = None,
        status: Sequence[str] | None = None,
        tier: Tier | None = None,
        subject: tuple[str, str] | None = None,
        as_of: datetime | None = None,
        text: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
        compact_chars: None,
    ) -> list[Decision]: ...

    async def relevant(
        self,
        *,
        domains: Sequence[str] | None = None,
        status: Sequence[str] | None = None,
        tier: Tier | None = None,
        subject: tuple[str, str] | None = None,
        as_of: datetime | None = None,
        text: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
        compact_chars: int | None = 200,
    ) -> list[CompactDecision] | list[Decision]:
        """FR-6.1: decisions matching `domains` (default all), `status` (default
        `{"current"}`), `tier`, and `subject` — subject-ref match **or** unscoped
        (no subject refs at all). `as_of` filters `valid_from`/`valid_until`
        (default now, excluding decisions whose `valid_until` has passed). `text`
        is an ILIKE substring filter over scenario/outcome/reasoning.
        `include_archived` adds `"archived"` to the effective status set.

        Ordered deterministically: recency (`recorded_at` descending), then id.

        Projection: `compact_chars` an `int` returns `list[CompactDecision]` with
        `outcome` truncated to that length **in SQL** (no fetch-then-trim, FR-6.7);
        `compact_chars=None` returns the full `list[Decision]`. The two
        `@overload`s above narrow the return type at each call site on a
        literal/omitted vs. `None` `compact_chars`, so callers get
        `list[CompactDecision]`/`list[Decision]` without a cast; this last
        signature is the Protocol's structurally-checked one, and conforming
        implementations (e.g. `PostgresStore.relevant`) carry the same
        overload pair plus one real implementation.
        """
        ...

    async def history(self, decision_id: UUID) -> HistoryRecord:
        """FR-6.2: the decision's full record — content, refs, transitions (in
        order), every link touching it, both supersession chains (recursive over
        `links` kind SUPERSEDES), and its supplements. Includes archived/discarded
        decisions throughout (history hides nothing).

        Raises:
            DecisionNotFound: no decision has `decision_id`.
        """
        ...

    async def changes(
        self,
        since: datetime | None = None,
        actions: Sequence[str] | None = None,
        actor: Actor | None = None,
        limit: int = 500,
    ) -> list[tuple[Transition, CompactDecision]]:
        """FR-6.5: transitions filtered by window (`since`), `actions`, and
        `actor`, each paired with its decision's compact projection. Most-recent
        first, capped at `limit`."""
        ...

    async def open_queue(
        self,
        kinds: Sequence[str] | None = None,
        order: Literal["oldest", "shakiest", "domain"] = "oldest",
    ) -> list[QueueItemView]:
        """FR-4.3/6.4: open (unresolved) queue items, optionally restricted to
        `kinds`. `oldest` sorts by `proposed_at` ascending; `domain` by the
        subject decision's domain; `shakiest` by confidence ascending — the
        item's own `confidence`, else the subject decision's `confidence`, else
        `1.0` (sorted last)."""
        ...

    async def by_source(self, source: str, **filters: Any) -> list[CompactDecision]:
        """FR-6.8: a source system's own decisions (`decisions.source = source`),
        with the standard `status`/`tier`/`limit`/`compact_chars` filters accepted
        as keyword arguments.

        Raises:
            TypeError: an unrecognized filter keyword was supplied.
        """
        ...

    async def knn(
        self, vector: list[float], k: int, *, exclude_ids: Sequence[UUID] = ()
    ) -> list[tuple[UUID, float]]:
        """FR-6.3/6.9: the `k` nearest decisions to `vector` by pgvector cosine
        distance (similarity = 1 - distance), joined to `decisions` to exclude
        archived/discarded. Internally over-fetches `k * 4` rows before trimming
        to `k`, so post-filtering never starves the result. `exclude_ids` removes
        specific decisions (e.g. the query's own source) from consideration."""
        ...

    async def unembedded(self, limit: int) -> list[Decision]:
        """FR-6.9: decisions with no `embeddings` row (the backfill backlog),
        oldest-recorded first, capped at `limit`."""
        ...

    async def undiscovered(self, limit: int) -> list[UUID]:
        """FR-7.4: embedded decisions with `discovered_at IS NULL` (the discovery
        cursor), oldest-embedded first, capped at `limit`."""
        ...

    async def aging_unrecommended(self, older_than: datetime, limit: int) -> list[CompactDecision]:
        """FR-6.9: short-term `current` decisions recorded before `older_than`
        with no open `promote` queue item, oldest-recorded first, capped at
        `limit`."""
        ...

    async def archival_eligible(self, cutoff: datetime) -> list[UUID]:
        """FR-3.4: short-term `current`/`not_promoted` decisions recorded before
        `cutoff` with no open queue item referencing them (as either the item's
        decision or its target) — an open item stops the archival clock — AND
        no non-`recorded` transition at or after `cutoff` (a later act, e.g.
        `reactivate()`, also stops the clock even though it doesn't change
        `recorded_at`; the row's own origination `recorded` transition is
        excluded, since it isn't a "touch since")."""
        ...

    async def export_rows(
        self,
        *,
        domains: Sequence[str] | None = None,
        tier: Tier | None = None,
        status: Sequence[str] | None = None,
    ) -> ExportBundle:
        """FR-6.6: decisions matching the filters (each carrying its own refs),
        every link and transition touching them, and the full domains registry.
        Embeddings are deliberately excluded (derived, rebuildable)."""
        ...
