"""PostgreSQL store adapter (ARCHITECTURE.md §4 / §4.1; docs/components/02-store-and-migrations.md).

The only place binnacle touches PostgreSQL: migrations, transactions, and the
write primitives the Lifecycle Engine composes above this layer. All objects
live under a constructor-supplied `schema_name` (§4.1's ownership boundary).

Migration mechanics (both documented here since they're exercised directly by
tests/db/test_migrations.py, not only through `migrate()`):

- yoyo is sync; `migrate()` is async, so the actual work runs via
  `asyncio.to_thread` (stdlib — the plan's controller notes suggested
  `anyio.to_thread.run_sync`, but anyio isn't a project dependency and adding one
  for a single blocking call is unjustified per GUIDELINES.md §7; the stdlib
  equivalent does the same job with the same semantics for one self-contained
  blocking operation).
- `{schema}`/`{embedding_dim}` are templated into the packaged .sql files by
  plain `str.replace` (not `.format` — the JSONB defaults in 0001_schema.sql
  contain literal `{}` that `.format` would misparse as a field reference) into
  a temp directory yoyo reads from.
- Two schemas coexisting in one database requires yoyo's OWN bookkeeping tables
  (migration/lock/log/version) to be schema-scoped too, or applying migrations
  for schema B would see schema A's migration ids already marked applied and
  skip them. yoyo's psycopg backend supports exactly this via a `schema` URI
  query param that runs `SET search_path TO {schema}` on connect — used here via
  `_yoyo_uri` — which is why the schema must already exist (a preflight step)
  before the yoyo connection is opened.
"""

import asyncio
import re
import tempfile
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from yoyo import get_backend, read_migrations

from binnacle.application.ports import DecisionRow, InsertOutcome, StorePort, Tx
from binnacle.domain.errors import (
    ConfigError,
    DecisionNotFound,
    EmbeddingDimensionMismatch,
    IdempotencyConflict,
    ItemAlreadyResolved,
    ItemNotFound,
)
from binnacle.domain.models import (
    Actor,
    CompactDecision,
    Decision,
    DomainRecord,
    ExportBundle,
    HistoryRecord,
    Link,
    LinkKind,
    OptionConsidered,
    QueueItem,
    QueueItemView,
    QueueKind,
    Ref,
    Tier,
    Transition,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_QUEUE_DEDUP_TARGET = (
    "(kind, decision_id, COALESCE(target_id, '00000000-0000-0000-0000-000000000000'::uuid))"
    " WHERE NOT resolved"
)

# FR-6.7 default compact-projection truncation length.
_DEFAULT_COMPACT_CHARS = 200

# FR-6.1 default relevance status set — "current" only, unless widened by an
# explicit `status` filter or `include_archived`.
_DEFAULT_RELEVANT_STATUS = frozenset({"current"})

# FR-6.6 export document schema version (the bundle's own JSON shape, distinct
# from `decisions.schema_version`).
_EXPORT_SCHEMA_VERSION = 1

# history()'s supersession-chain CTEs walk `links` kind SUPERSEDES, which the
# Lifecycle Engine keeps acyclic by construction (docs/ARCHITECTURE.md §4: "checked
# in Lifecycle Engine via chain walk, not a DB constraint") — but that engine
# doesn't exist yet, and even once it does, a read path shouldn't trust writes it
# didn't validate itself (defense-in-depth). Both CTEs below track visited ids and
# stop on revisit (correct termination for any finite graph, cyclic or not); this
# cap is an extra hard stop in case that tracking is ever wrong.
_HISTORY_MAX_DEPTH = 64

# `self._schema` is interpolated directly into every DDL/DML f-string below
# (psycopg has no bind-parameter syntax for identifiers), so an unvalidated
# value is a SQL-injection vector. `BinnacleConfig` validates this same
# pattern, but `PostgresStore` is constructible directly (bypassing that
# config object entirely, e.g. in tests), so it re-validates here rather than
# trusting every caller to have gone through `BinnacleConfig` first.
_SCHEMA_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class _PgTx(Tx):
    """Concrete `Tx`: carries the psycopg connection for one write transaction."""

    __slots__ = ("conn",)

    def __init__(self, conn: "psycopg.AsyncConnection[Any]") -> None:
        self.conn = conn


def _yoyo_uri(dsn: str, schema: str) -> str:
    """A yoyo connection URI for the psycopg3 backend (`postgresql+psycopg`), scoped
    to `schema` via yoyo's built-in `schema` query param (see module docstring).

    `public` stays on the search_path behind `schema` so unqualified type lookups
    (the `vector` type from pgvector, typically installed in `public` by the
    provisioning operator per §4.1) keep resolving — our own DDL never relies on
    search_path for table/index names (those are always `{schema}`-qualified), only
    yoyo's own bookkeeping tables and Postgres's built-in type lookups do.
    """
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query))
    query["schema"] = f"{schema}, public"
    return urlunsplit(("postgresql+psycopg", parts.netloc, parts.path, urlencode(query), ""))


def _render_migrations(dest: Path, schema: str, embedding_dim: int) -> None:
    """Copy the packaged migration files into `dest`, substituting `{schema}` and
    `{embedding_dim}` placeholders (plain string replacement — see module docstring)."""
    for src in sorted(MIGRATIONS_DIR.glob("*.sql")):
        text = src.read_text()
        text = text.replace("{schema}", schema).replace("{embedding_dim}", str(embedding_dim))
        (dest / src.name).write_text(text)


def _row_to_queue_item(row: dict[str, Any]) -> QueueItem:
    return QueueItem(
        item_id=row["item_id"],
        kind=row["kind"],
        decision_id=row["decision_id"],
        target_id=row["target_id"],
        proposed_by=Actor.from_str(row["proposed_by"]),
        proposed_at=row["proposed_at"],
        rationale=row["rationale"],
        confidence=row["confidence"],
        resolved=row["resolved"],
    )


def _row_to_transition(row: dict[str, Any]) -> Transition:
    return Transition(
        transition_id=row["transition_id"],
        decision_id=row["decision_id"],
        action=row["action"],
        actor=Actor.from_str(row["actor"]),
        at=row["at"],
        reason=row["reason"],
        new_status=row["new_status"],
        payload=row["payload"],
    )


def _row_to_decision(
    row: dict[str, Any], refs: list[Ref], links: list[tuple[UUID, str]]
) -> Decision:
    return Decision(
        decision_id=row["decision_id"],
        domain=row["domain"],
        tier=row["tier"],
        status=row["status"],
        scenario=row["scenario"],
        outcome=row["outcome"],
        reasoning=row["reasoning"],
        source=row["source"],
        recorded_by=Actor.from_str(row["recorded_by"]),
        recorded_at=row["recorded_at"],
        decided_at=row["decided_at"],
        options_considered=[
            OptionConsidered(option=o["option"], why_rejected=o["why_rejected"])
            for o in row["options_considered"]
        ],
        consequences=row["consequences"],
        confidence=row["confidence"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        refs=refs,
        supersedes=[to_id for to_id, kind in links if kind == "SUPERSEDES"],
        supplements=[to_id for to_id, kind in links if kind == "SUPPLEMENTS"],
        metadata=row["metadata"],
        schema_version=row["schema_version"],
    )


def _escape_ilike(text: str) -> str:
    """Escape `text` for use inside an ILIKE pattern (`relevant()`'s lexical
    filter, FR-6.1): backslash first (so it doesn't double-escape the `%`/`_`
    escapes it introduces), then `%` and `_`, Postgres's ILIKE metacharacters.
    Postgres's default `ESCAPE` character is already backslash, so callers just
    wrap the result in `%...%` — no explicit `ESCAPE` clause needed."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PostgresStore:
    """The only adapter touching PostgreSQL. See docs/components/02-store-and-migrations.md."""

    def __init__(
        self,
        *,
        dsn: str | None = None,
        pool: AsyncConnectionPool | None = None,
        schema_name: str = "binnacle",
        embedding_dim: int = 768,
    ) -> None:
        """Construct with exactly one of `dsn` or `pool`.

        Raises:
            ConfigError: both or neither of `dsn`/`pool` were supplied, or
                `schema_name` is not a valid identifier.
        """
        if (dsn is None) == (pool is None):
            msg = "PostgresStore requires exactly one of dsn or pool"
            raise ConfigError(msg)
        if not _SCHEMA_NAME_RE.match(schema_name):
            msg = (
                f"schema_name {schema_name!r} is not a valid identifier "
                f"(must match {_SCHEMA_NAME_RE.pattern!r})"
            )
            raise ConfigError(msg)
        self._dsn = dsn
        self._pool = pool
        self._owns_pool = pool is None
        self._schema = schema_name
        self._embedding_dim = embedding_dim

    async def aclose(self) -> None:
        """Close the pool this store opened itself; a no-op given a caller-supplied pool."""
        if self._owns_pool and self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _ensure_pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            assert self._dsn is not None
            pool = AsyncConnectionPool(
                self._dsn,
                kwargs={"row_factory": dict_row},
                min_size=1,
                max_size=4,
                open=False,
            )
            await pool.open()
            self._pool = pool
        return self._pool

    @staticmethod
    def _conn(tx: Tx) -> "psycopg.AsyncConnection[Any]":
        assert isinstance(tx, _PgTx)
        return tx.conn

    # -- migrations -----------------------------------------------------------

    async def migrate(self) -> None:
        """Apply pending migrations, then verify the migrated VECTOR(n) dimension
        matches `embedding_dim` (docs/components/02-store-and-migrations.md).

        Raises:
            ConfigError: the pgvector extension is not installed (a provisioning
                precondition binnacle checks and reports, never performs — §4.1).
            EmbeddingDimensionMismatch: the migrated `embeddings.embedding` column's
                dimension does not equal the configured `embedding_dim`.
        """
        assert self._dsn is not None, "migrate() requires a dsn (yoyo needs its own connection)"
        await asyncio.to_thread(self._migrate_sync, self._dsn, self._schema, self._embedding_dim)

    @staticmethod
    def _migrate_sync(dsn: str, schema: str, embedding_dim: int) -> None:
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            if cur.fetchone() is None:
                msg = (
                    "pgvector extension not installed — provisioning precondition; "
                    "ask the database operator to run CREATE EXTENSION vector"
                )
                raise ConfigError(msg)
            # Must exist before the yoyo connection below sets search_path to it.
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

        with tempfile.TemporaryDirectory() as tmp:
            _render_migrations(Path(tmp), schema, embedding_dim)
            backend = get_backend(_yoyo_uri(dsn, schema))
            try:
                migrations = read_migrations(tmp)
                with backend.lock():
                    backend.apply_migrations(backend.to_apply(migrations))
            finally:
                backend.connection.close()

        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT a.atttypmod FROM pg_attribute a "
                "JOIN pg_class c ON a.attrelid = c.oid "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE n.nspname = %s AND c.relname = 'embeddings' AND a.attname = 'embedding'",
                (schema,),
            )
            row = cur.fetchone()
            actual = row[0] if row else None
            if actual != embedding_dim:
                msg = f"embeddings.embedding is VECTOR({actual}), configured embedding_dim={embedding_dim}"
                raise EmbeddingDimensionMismatch(msg)

    # -- transactions -----------------------------------------------------------

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Tx]:
        """Open one write transaction. Use as `async with store.transaction() as tx:`."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn, conn.transaction():
            yield _PgTx(conn)

    @asynccontextmanager
    async def _read_conn(self) -> AsyncIterator["psycopg.AsyncConnection[Any]"]:
        """A plain pooled connection for one read (no transaction/lock needed —
        the reads in this module never mutate). Declared `AsyncConnection[Any]`
        (matching `_conn`'s cast for writes) since `AsyncConnectionPool` isn't
        parameterized with the pool's actual `dict_row` row factory (set at
        runtime via `kwargs`, invisible to the type checker)."""
        pool = await self._ensure_pool()
        async with pool.connection() as conn:
            yield conn

    # -- write primitives -----------------------------------------------------------

    async def lock_decisions(self, tx: Tx, ids: Sequence[UUID]) -> dict[UUID, DecisionRow]:
        ordered = sorted(set(ids))
        if not ordered:
            return {}
        conn = self._conn(tx)
        cur = await conn.execute(
            f"SELECT decision_id, tier, domain, status FROM {self._schema}.decisions "
            "WHERE decision_id = ANY(%s) ORDER BY decision_id FOR UPDATE",
            (ordered,),
        )
        return {
            row["decision_id"]: DecisionRow(
                decision_id=row["decision_id"],
                tier=row["tier"],
                domain=row["domain"],
                status=row["status"],
            )
            async for row in cur
        }

    async def predecessor_chain(self, tx: Tx, decision_id: UUID) -> list[UUID]:
        conn = self._conn(tx)
        cur = await conn.execute(
            f"WITH RECURSIVE pred AS ("
            f"  SELECT to_id AS decision_id, 1 AS depth, ARRAY[from_id, to_id] AS visited"
            f"  FROM {self._schema}.links WHERE from_id = %(id)s AND kind = 'SUPERSEDES'"
            "  UNION ALL"
            f"  SELECT l.to_id, p.depth + 1, p.visited || l.to_id"
            f"  FROM {self._schema}.links l JOIN pred p ON l.from_id = p.decision_id"
            "   WHERE l.kind = 'SUPERSEDES' AND NOT (l.to_id = ANY(p.visited))"
            "   AND p.depth < %(max_depth)s"
            ") SELECT decision_id FROM pred ORDER BY depth",
            {"id": decision_id, "max_depth": _HISTORY_MAX_DEPTH},
        )
        return [row["decision_id"] async for row in cur]

    async def get_decision_tx(self, tx: Tx, decision_id: UUID) -> Decision | None:
        conn = self._conn(tx)
        cur = await conn.execute(
            f"SELECT * FROM {self._schema}.decisions WHERE decision_id = %s", (decision_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        decisions = await self._hydrate_decisions(conn, [row])
        return decisions[0]

    async def transitions_for(self, tx: Tx, decision_id: UUID) -> list[Transition]:
        conn = self._conn(tx)
        cur = await conn.execute(
            f"SELECT * FROM {self._schema}.transitions WHERE decision_id = %s "
            "ORDER BY at ASC, transition_id ASC",
            (decision_id,),
        )
        return [_row_to_transition(row) async for row in cur]

    async def insert_decision(self, tx: Tx, d: Decision, content_hash: str) -> InsertOutcome:
        conn = self._conn(tx)
        decided_at = d.decided_at if d.decided_at is not None else d.recorded_at
        options = Jsonb(
            [{"option": o.option, "why_rejected": o.why_rejected} for o in d.options_considered]
        )
        cur = await conn.execute(
            f"INSERT INTO {self._schema}.decisions ("
            "  decision_id, tier, domain, status, scenario, outcome, reasoning,"
            "  options_considered, consequences, confidence, source, content_hash,"
            "  recorded_by, decided_at, recorded_at, valid_from, valid_until,"
            "  metadata, schema_version"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (decision_id) DO NOTHING",
            (
                d.decision_id,
                d.tier,
                d.domain,
                d.status,
                d.scenario,
                d.outcome,
                d.reasoning,
                options,
                d.consequences,
                d.confidence,
                d.source,
                content_hash,
                d.recorded_by.as_str(),
                decided_at,
                d.recorded_at,
                d.valid_from,
                d.valid_until,
                Jsonb(d.metadata),
                d.schema_version,
            ),
        )
        if cur.rowcount == 1:
            return "inserted"
        existing = await conn.execute(
            f"SELECT content_hash FROM {self._schema}.decisions WHERE decision_id = %s",
            (d.decision_id,),
        )
        row = await existing.fetchone()
        assert row is not None, "ON CONFLICT fired, so a row with this decision_id must exist"
        if row["content_hash"] == content_hash:
            return "exists_identical"
        msg = f"decision {d.decision_id} already recorded with a different content hash"
        raise IdempotencyConflict(msg)

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
        conn = self._conn(tx)
        await conn.execute(
            f"INSERT INTO {self._schema}.transitions "
            "(decision_id, action, actor, reason, new_status, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                decision_id,
                action,
                actor,
                reason,
                new_status,
                Jsonb(payload) if payload is not None else None,
            ),
        )
        if new_status is not None:
            await conn.execute(
                f"UPDATE {self._schema}.decisions SET status = %s WHERE decision_id = %s",
                (new_status, decision_id),
            )

    async def insert_link(self, tx: Tx, from_id: UUID, to_id: UUID, kind: LinkKind) -> None:
        conn = self._conn(tx)
        await conn.execute(
            f"INSERT INTO {self._schema}.links (from_id, to_id, kind) VALUES (%s, %s, %s) "
            "ON CONFLICT (from_id, kind, to_id) DO NOTHING",
            (from_id, to_id, kind),
        )

    async def insert_refs(self, tx: Tx, decision_id: UUID, refs: Sequence[Ref]) -> None:
        if not refs:
            return
        conn = self._conn(tx)
        async with conn.cursor() as cur:
            await cur.executemany(
                f"INSERT INTO {self._schema}.refs (decision_id, role, kind, identifier, note) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (decision_id, role, kind, identifier) DO NOTHING",
                [(decision_id, r.role, r.kind, r.identifier, r.note) for r in refs],
            )

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
        conn = self._conn(tx)
        cur = await conn.execute(
            f"INSERT INTO {self._schema}.queue "
            "(kind, decision_id, target_id, proposed_by, proposed_at, rationale, confidence) "
            "VALUES (%s, %s, %s, %s, now(), %s, %s) "
            f"ON CONFLICT {_QUEUE_DEDUP_TARGET} DO NOTHING "
            "RETURNING item_id",
            (kind, decision_id, target_id, proposed_by.as_str(), rationale, confidence),
        )
        row = await cur.fetchone()
        return row["item_id"] if row is not None else None

    async def resolve_item(self, tx: Tx, item_id: int) -> QueueItem:
        conn = self._conn(tx)
        cur = await conn.execute(
            f"UPDATE {self._schema}.queue SET resolved = TRUE "
            "WHERE item_id = %s AND NOT resolved RETURNING *",
            (item_id,),
        )
        row = await cur.fetchone()
        if row is None:
            exists = await conn.execute(
                f"SELECT 1 FROM {self._schema}.queue WHERE item_id = %s", (item_id,)
            )
            if await exists.fetchone() is None:
                msg = f"queue item {item_id} not found"
                raise ItemNotFound(msg)
            msg = f"queue item {item_id} already resolved"
            raise ItemAlreadyResolved(msg)
        return _row_to_queue_item(row)

    async def open_items_for(self, tx: Tx, decision_id: UUID) -> list[QueueItem]:
        conn = self._conn(tx)
        cur = await conn.execute(
            f"SELECT * FROM {self._schema}.queue "
            "WHERE decision_id = %s AND NOT resolved ORDER BY proposed_at",
            (decision_id,),
        )
        return [_row_to_queue_item(row) async for row in cur]

    async def domain_exists(self, conn_or_tx: Tx, name: str) -> bool:
        conn = self._conn(conn_or_tx)
        cur = await conn.execute(f"SELECT 1 FROM {self._schema}.domains WHERE name = %s", (name,))
        return await cur.fetchone() is not None

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
        conn = self._conn(tx)
        await conn.execute(
            f"INSERT INTO {self._schema}.domains (name, description, active) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET "
            "description = EXCLUDED.description, active = EXCLUDED.active",
            (name, description, active),
        )
        await conn.execute(
            f"INSERT INTO {self._schema}.domain_transitions (domain, action, actor, reason) "
            "VALUES (%s, %s, %s, %s)",
            (name, action, actor, reason),
        )

    async def upsert_embedding(self, tx: Tx, decision_id: UUID, vector: list[float]) -> None:
        if len(vector) != self._embedding_dim:
            msg = f"embedding has {len(vector)} dims, expected {self._embedding_dim}"
            raise EmbeddingDimensionMismatch(msg)
        conn = self._conn(tx)
        await register_vector_async(conn)
        await conn.execute(
            f"INSERT INTO {self._schema}.embeddings (decision_id, embedding, embedded_at, discovered_at) "
            "VALUES (%s, %s, now(), NULL) "
            "ON CONFLICT (decision_id) DO UPDATE SET "
            "embedding = EXCLUDED.embedding, embedded_at = EXCLUDED.embedded_at",
            (decision_id, Vector(vector)),
        )

    async def mark_discovered(self, tx: Tx, decision_ids: Sequence[UUID]) -> None:
        if not decision_ids:
            return
        conn = self._conn(tx)
        await conn.execute(
            f"UPDATE {self._schema}.embeddings SET discovered_at = now() "
            "WHERE decision_id = ANY(%s)",
            (list(decision_ids),),
        )

    # -- reads -----------------------------------------------------------------
    # Read-only: a plain pooled connection is enough, no transaction/lock needed.

    async def _hydrate_decisions(
        self, conn: "psycopg.AsyncConnection[Any]", rows: Sequence[dict[str, Any]]
    ) -> list[Decision]:
        """Batch-attach refs and declared supersedes/supplements links to `rows`
        (each a full `decisions` row), preserving `rows`' order."""
        if not rows:
            return []
        ids = [r["decision_id"] for r in rows]
        refs_by_id: dict[UUID, list[Ref]] = defaultdict(list)
        cur = await conn.execute(
            f"SELECT decision_id, role, kind, identifier, note FROM {self._schema}.refs "
            "WHERE decision_id = ANY(%s)",
            (ids,),
        )
        async for r in cur:
            refs_by_id[r["decision_id"]].append(
                Ref(role=r["role"], kind=r["kind"], identifier=r["identifier"], note=r["note"])
            )
        links_by_id: dict[UUID, list[tuple[UUID, str]]] = defaultdict(list)
        cur = await conn.execute(
            f"SELECT from_id, to_id, kind FROM {self._schema}.links "
            "WHERE from_id = ANY(%s) AND kind IN ('SUPERSEDES', 'SUPPLEMENTS')",
            (ids,),
        )
        async for r in cur:
            links_by_id[r["from_id"]].append((r["to_id"], r["kind"]))
        return [
            _row_to_decision(
                r, refs_by_id.get(r["decision_id"], []), links_by_id.get(r["decision_id"], [])
            )
            for r in rows
        ]

    async def _decisions_by_id_ordered(
        self, conn: "psycopg.AsyncConnection[Any]", ids: list[UUID]
    ) -> list[Decision]:
        """`get_many`, but preserving `ids`' order (for the supersession chains,
        where order is nearest-to-farthest, not incidental)."""
        if not ids:
            return []
        cur = await conn.execute(
            f"SELECT * FROM {self._schema}.decisions WHERE decision_id = ANY(%s)", (ids,)
        )
        rows_by_id = {r["decision_id"]: r async for r in cur}
        ordered_rows = [rows_by_id[i] for i in ids if i in rows_by_id]
        return await self._hydrate_decisions(conn, ordered_rows)

    async def _fetch_subject_refs(
        self, conn: "psycopg.AsyncConnection[Any]", ids: Sequence[UUID]
    ) -> dict[UUID, list[Ref]]:
        refs_by_id: dict[UUID, list[Ref]] = defaultdict(list)
        if not ids:
            return refs_by_id
        cur = await conn.execute(
            f"SELECT decision_id, kind, identifier, note FROM {self._schema}.refs "
            "WHERE decision_id = ANY(%s) AND role = 'subject'",
            (list(ids),),
        )
        async for r in cur:
            refs_by_id[r["decision_id"]].append(
                Ref(role="subject", kind=r["kind"], identifier=r["identifier"], note=r["note"])
            )
        return refs_by_id

    async def list_domains(self) -> list[DomainRecord]:
        async with self._read_conn() as conn:
            cur = await conn.execute(
                f"SELECT name, description, active FROM {self._schema}.domains ORDER BY name"
            )
            rows = await cur.fetchall()
        return [
            DomainRecord(name=r["name"], description=r["description"], active=r["active"])
            for r in rows
        ]

    async def get_decision(self, decision_id: UUID) -> Decision | None:
        async with self._read_conn() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {self._schema}.decisions WHERE decision_id = %s", (decision_id,)
            )
            row = await cur.fetchone()
            if row is None:
                return None
            decisions = await self._hydrate_decisions(conn, [row])
        return decisions[0]

    async def get_many(self, ids: Sequence[UUID]) -> list[Decision]:
        if not ids:
            return []
        async with self._read_conn() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {self._schema}.decisions WHERE decision_id = ANY(%s) "
                "ORDER BY decision_id",
                (list(ids),),
            )
            rows = await cur.fetchall()
            return await self._hydrate_decisions(conn, rows)

    async def get_many_compact(
        self, ids: Sequence[UUID], *, compact_chars: int = 200
    ) -> list[CompactDecision]:
        if not ids:
            return []
        schema = self._schema
        params: dict[str, Any] = {"ids": list(ids), "compact_chars": compact_chars}
        sql = (
            f"SELECT d.decision_id, d.domain, d.tier, d.status, "
            f"LEFT(d.outcome, %(compact_chars)s) AS outcome_truncated "
            f"FROM {schema}.decisions d WHERE d.decision_id = ANY(%(ids)s)"
        )
        async with self._read_conn() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            subject_refs_by_id = await self._fetch_subject_refs(
                conn, [r["decision_id"] for r in rows]
            )
        return [
            CompactDecision(
                id=r["decision_id"],
                domain=r["domain"],
                tier=r["tier"],
                status=r["status"],
                outcome_truncated=r["outcome_truncated"],
                subject_refs=subject_refs_by_id.get(r["decision_id"], []),
            )
            for r in rows
        ]

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
    ) -> "list[CompactDecision] | list[Decision]":
        schema = self._schema
        statuses = set(status) if status is not None else set(_DEFAULT_RELEVANT_STATUS)
        if include_archived:
            statuses.add("archived")
        effective_as_of = as_of if as_of is not None else datetime.now(UTC)

        conditions = ["d.status = ANY(%(statuses)s)"]
        params: dict[str, Any] = {
            "statuses": list(statuses),
            "as_of": effective_as_of,
            "limit": limit,
        }
        conditions.append("(d.valid_from IS NULL OR d.valid_from <= %(as_of)s)")
        conditions.append("(d.valid_until IS NULL OR d.valid_until > %(as_of)s)")
        if domains is not None:
            conditions.append("d.domain = ANY(%(domains)s)")
            params["domains"] = list(domains)
        if tier is not None:
            conditions.append("d.tier = %(tier)s")
            params["tier"] = tier
        if subject is not None:
            subj_kind, subj_id = subject
            conditions.append(
                f"(EXISTS (SELECT 1 FROM {schema}.refs r WHERE r.decision_id = d.decision_id "
                "AND r.role = 'subject' AND r.kind = %(subj_kind)s AND r.identifier = %(subj_id)s) "
                f"OR NOT EXISTS (SELECT 1 FROM {schema}.refs r2 "
                "WHERE r2.decision_id = d.decision_id AND r2.role = 'subject'))"
            )
            params["subj_kind"] = subj_kind
            params["subj_id"] = subj_id
        if text is not None:
            conditions.append(
                "(d.scenario ILIKE %(text)s OR d.outcome ILIKE %(text)s OR d.reasoning ILIKE %(text)s)"
            )
            params["text"] = f"%{_escape_ilike(text)}%"
        where_sql = " AND ".join(conditions)

        if compact_chars is not None:
            params["compact_chars"] = compact_chars
            sql = (
                f"SELECT d.decision_id, d.domain, d.tier, d.status, "
                f"LEFT(d.outcome, %(compact_chars)s) AS outcome_truncated "
                f"FROM {schema}.decisions d WHERE {where_sql} "
                "ORDER BY d.recorded_at DESC, d.decision_id ASC LIMIT %(limit)s"
            )
            async with self._read_conn() as conn:
                cur = await conn.execute(sql, params)
                rows = await cur.fetchall()
                subject_refs_by_id = await self._fetch_subject_refs(
                    conn, [r["decision_id"] for r in rows]
                )
            return [
                CompactDecision(
                    id=r["decision_id"],
                    domain=r["domain"],
                    tier=r["tier"],
                    status=r["status"],
                    outcome_truncated=r["outcome_truncated"],
                    subject_refs=subject_refs_by_id.get(r["decision_id"], []),
                )
                for r in rows
            ]

        sql = (
            f"SELECT d.* FROM {schema}.decisions d WHERE {where_sql} "
            "ORDER BY d.recorded_at DESC, d.decision_id ASC LIMIT %(limit)s"
        )
        async with self._read_conn() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            return await self._hydrate_decisions(conn, rows)

    async def history(self, decision_id: UUID) -> HistoryRecord:
        schema = self._schema
        async with self._read_conn() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {schema}.decisions WHERE decision_id = %s", (decision_id,)
            )
            row = await cur.fetchone()
            if row is None:
                msg = f"decision {decision_id} not found"
                raise DecisionNotFound(msg)
            decision = (await self._hydrate_decisions(conn, [row]))[0]

            cur = await conn.execute(
                f"SELECT * FROM {schema}.transitions WHERE decision_id = %s "
                "ORDER BY at ASC, transition_id ASC",
                (decision_id,),
            )
            transitions = [_row_to_transition(r) async for r in cur]

            cur = await conn.execute(
                f"SELECT from_id, to_id, kind FROM {schema}.links WHERE from_id = %s OR to_id = %s",
                (decision_id, decision_id),
            )
            links = [
                Link(from_id=r["from_id"], to_id=r["to_id"], kind=r["kind"]) async for r in cur
            ]

            cur = await conn.execute(
                f"WITH RECURSIVE pred AS ("
                f"  SELECT to_id AS decision_id, 1 AS depth, ARRAY[from_id, to_id] AS visited"
                f"  FROM {schema}.links WHERE from_id = %(id)s AND kind = 'SUPERSEDES'"
                "  UNION ALL"
                f"  SELECT l.to_id, p.depth + 1, p.visited || l.to_id"
                f"  FROM {schema}.links l JOIN pred p ON l.from_id = p.decision_id"
                "   WHERE l.kind = 'SUPERSEDES' AND NOT (l.to_id = ANY(p.visited))"
                "   AND p.depth < %(max_depth)s"
                ") SELECT decision_id FROM pred ORDER BY depth",
                {"id": decision_id, "max_depth": _HISTORY_MAX_DEPTH},
            )
            predecessor_ids = [r["decision_id"] async for r in cur]

            cur = await conn.execute(
                f"WITH RECURSIVE succ AS ("
                f"  SELECT from_id AS decision_id, 1 AS depth, ARRAY[to_id, from_id] AS visited"
                f"  FROM {schema}.links WHERE to_id = %(id)s AND kind = 'SUPERSEDES'"
                "  UNION ALL"
                f"  SELECT l.from_id, s.depth + 1, s.visited || l.from_id"
                f"  FROM {schema}.links l JOIN succ s ON l.to_id = s.decision_id"
                "   WHERE l.kind = 'SUPERSEDES' AND NOT (l.from_id = ANY(s.visited))"
                "   AND s.depth < %(max_depth)s"
                ") SELECT decision_id FROM succ ORDER BY depth",
                {"id": decision_id, "max_depth": _HISTORY_MAX_DEPTH},
            )
            successor_ids = [r["decision_id"] async for r in cur]

            cur = await conn.execute(
                f"SELECT from_id FROM {schema}.links WHERE to_id = %s AND kind = 'SUPPLEMENTS'",
                (decision_id,),
            )
            supplement_ids = [r["from_id"] async for r in cur]

            predecessors = await self._decisions_by_id_ordered(conn, predecessor_ids)
            successors = await self._decisions_by_id_ordered(conn, successor_ids)
            supplements = await self._decisions_by_id_ordered(conn, supplement_ids)

        return HistoryRecord(
            decision=decision,
            transitions=transitions,
            links=links,
            predecessors=predecessors,
            successors=successors,
            supplements=supplements,
        )

    async def changes(
        self,
        since: datetime | None = None,
        actions: Sequence[str] | None = None,
        actor: Actor | None = None,
    ) -> list[tuple[Transition, CompactDecision]]:
        schema = self._schema
        conditions = []
        params: dict[str, Any] = {"compact_chars": _DEFAULT_COMPACT_CHARS}
        if since is not None:
            conditions.append("t.at >= %(since)s")
            params["since"] = since
        if actions is not None:
            conditions.append("t.action = ANY(%(actions)s)")
            params["actions"] = list(actions)
        if actor is not None:
            conditions.append("t.actor = %(actor)s")
            params["actor"] = actor.as_str()
        where_sql = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            "SELECT t.*, d.domain AS d_domain, d.tier AS d_tier, d.status AS d_status, "
            f"LEFT(d.outcome, %(compact_chars)s) AS d_outcome_truncated "
            f"FROM {schema}.transitions t JOIN {schema}.decisions d ON d.decision_id = t.decision_id"
            f"{where_sql} ORDER BY t.at DESC, t.transition_id DESC"
        )
        async with self._read_conn() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            subject_refs_by_id = await self._fetch_subject_refs(
                conn, [r["decision_id"] for r in rows]
            )
        return [
            (
                _row_to_transition(r),
                CompactDecision(
                    id=r["decision_id"],
                    domain=r["d_domain"],
                    tier=r["d_tier"],
                    status=r["d_status"],
                    outcome_truncated=r["d_outcome_truncated"],
                    subject_refs=subject_refs_by_id.get(r["decision_id"], []),
                ),
            )
            for r in rows
        ]

    async def open_queue(
        self,
        kinds: Sequence[str] | None = None,
        order: Literal["oldest", "shakiest", "domain"] = "oldest",
    ) -> list[QueueItemView]:
        schema = self._schema
        conditions = ["NOT q.resolved"]
        params: dict[str, Any] = {}
        if kinds is not None:
            conditions.append("q.kind = ANY(%(kinds)s)")
            params["kinds"] = list(kinds)
        order_sql = {
            "oldest": "q.proposed_at ASC, q.item_id ASC",
            # Ascending confidence: the fallback chain's ceiling (1.0, "absent")
            # naturally sorts last since confidence never exceeds 1.0.
            "shakiest": "COALESCE(q.confidence, d.confidence, 1.0) ASC, q.proposed_at ASC, q.item_id ASC",
            "domain": "d.domain ASC, q.proposed_at ASC, q.item_id ASC",
        }[order]
        sql = (
            "SELECT q.*, d.domain AS d_domain, d.confidence AS d_confidence "
            f"FROM {schema}.queue q JOIN {schema}.decisions d ON d.decision_id = q.decision_id "
            f"WHERE {' AND '.join(conditions)} ORDER BY {order_sql}"
        )
        async with self._read_conn() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
        now = datetime.now(UTC)
        return [
            QueueItemView(
                item=_row_to_queue_item(r),
                domain=r["d_domain"],
                decision_confidence=r["d_confidence"],
                age=now - r["proposed_at"],
            )
            for r in rows
        ]

    async def by_source(self, source: str, **filters: Any) -> list[CompactDecision]:
        status = filters.pop("status", None)
        tier = filters.pop("tier", None)
        limit = filters.pop("limit", 50)
        compact_chars = filters.pop("compact_chars", _DEFAULT_COMPACT_CHARS)
        if filters:
            msg = f"by_source() received unknown filters: {sorted(filters)}"
            raise TypeError(msg)

        schema = self._schema
        conditions = ["d.source = %(source)s"]
        params: dict[str, Any] = {
            "source": source,
            "compact_chars": compact_chars,
            "limit": limit,
        }
        if status is not None:
            conditions.append("d.status = ANY(%(status)s)")
            params["status"] = list(status)
        if tier is not None:
            conditions.append("d.tier = %(tier)s")
            params["tier"] = tier
        sql = (
            f"SELECT d.decision_id, d.domain, d.tier, d.status, "
            f"LEFT(d.outcome, %(compact_chars)s) AS outcome_truncated "
            f"FROM {schema}.decisions d WHERE {' AND '.join(conditions)} "
            "ORDER BY d.recorded_at DESC, d.decision_id ASC LIMIT %(limit)s"
        )
        async with self._read_conn() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            subject_refs_by_id = await self._fetch_subject_refs(
                conn, [r["decision_id"] for r in rows]
            )
        return [
            CompactDecision(
                id=r["decision_id"],
                domain=r["domain"],
                tier=r["tier"],
                status=r["status"],
                outcome_truncated=r["outcome_truncated"],
                subject_refs=subject_refs_by_id.get(r["decision_id"], []),
            )
            for r in rows
        ]

    async def knn(
        self, vector: list[float], k: int, *, exclude_ids: Sequence[UUID] = ()
    ) -> list[tuple[UUID, float]]:
        schema = self._schema
        conditions = ["d.status NOT IN ('archived', 'discarded')"]
        params: dict[str, Any] = {"vector": Vector(vector), "limit": k * 4}
        if exclude_ids:
            conditions.append("e.decision_id != ALL(%(exclude_ids)s)")
            params["exclude_ids"] = list(exclude_ids)
        sql = (
            "SELECT e.decision_id, e.embedding <=> %(vector)s AS distance "
            f"FROM {schema}.embeddings e JOIN {schema}.decisions d ON d.decision_id = e.decision_id "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY e.embedding <=> %(vector)s LIMIT %(limit)s"
        )
        async with self._read_conn() as conn:
            await register_vector_async(conn)
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
        results = [(r["decision_id"], 1.0 - r["distance"]) for r in rows]
        return results[:k]

    async def unembedded(self, limit: int) -> list[Decision]:
        schema = self._schema
        sql = (
            f"SELECT d.* FROM {schema}.decisions d "
            f"LEFT JOIN {schema}.embeddings e ON e.decision_id = d.decision_id "
            "WHERE e.decision_id IS NULL "
            "ORDER BY d.recorded_at ASC, d.decision_id ASC LIMIT %s"
        )
        async with self._read_conn() as conn:
            cur = await conn.execute(sql, (limit,))
            rows = await cur.fetchall()
            return await self._hydrate_decisions(conn, rows)

    async def undiscovered(self, limit: int) -> list[UUID]:
        schema = self._schema
        sql = (
            f"SELECT decision_id FROM {schema}.embeddings "
            "WHERE discovered_at IS NULL ORDER BY embedded_at ASC LIMIT %s"
        )
        async with self._read_conn() as conn:
            cur = await conn.execute(sql, (limit,))
            rows = await cur.fetchall()
        return [r["decision_id"] for r in rows]

    async def aging_unrecommended(self, older_than: datetime, limit: int) -> list[CompactDecision]:
        schema = self._schema
        sql = (
            f"SELECT d.decision_id, d.domain, d.tier, d.status, "
            f"LEFT(d.outcome, %(compact_chars)s) AS outcome_truncated "
            f"FROM {schema}.decisions d "
            "WHERE d.tier = 'short_term' AND d.status = 'current' "
            "AND d.recorded_at < %(older_than)s "
            f"AND NOT EXISTS (SELECT 1 FROM {schema}.queue q WHERE q.kind = 'promote' "
            "AND q.decision_id = d.decision_id AND NOT q.resolved) "
            "ORDER BY d.recorded_at ASC, d.decision_id ASC LIMIT %(limit)s"
        )
        params = {
            "older_than": older_than,
            "limit": limit,
            "compact_chars": _DEFAULT_COMPACT_CHARS,
        }
        async with self._read_conn() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            subject_refs_by_id = await self._fetch_subject_refs(
                conn, [r["decision_id"] for r in rows]
            )
        return [
            CompactDecision(
                id=r["decision_id"],
                domain=r["domain"],
                tier=r["tier"],
                status=r["status"],
                outcome_truncated=r["outcome_truncated"],
                subject_refs=subject_refs_by_id.get(r["decision_id"], []),
            )
            for r in rows
        ]

    async def archival_eligible(self, cutoff: datetime) -> list[UUID]:
        schema = self._schema
        sql = (
            f"SELECT d.decision_id FROM {schema}.decisions d "
            "WHERE d.tier = 'short_term' AND d.status IN ('current', 'not_promoted') "
            "AND d.recorded_at < %(cutoff)s "
            f"AND NOT EXISTS (SELECT 1 FROM {schema}.queue q WHERE NOT q.resolved "
            "AND (q.decision_id = d.decision_id OR q.target_id = d.decision_id)) "
            "ORDER BY d.recorded_at ASC, d.decision_id ASC"
        )
        async with self._read_conn() as conn:
            cur = await conn.execute(sql, {"cutoff": cutoff})
            rows = await cur.fetchall()
        return [r["decision_id"] for r in rows]

    async def export_rows(
        self,
        *,
        domains: Sequence[str] | None = None,
        tier: Tier | None = None,
        status: Sequence[str] | None = None,
    ) -> ExportBundle:
        schema = self._schema
        conditions = []
        params: dict[str, Any] = {}
        if domains is not None:
            conditions.append("d.domain = ANY(%(domains)s)")
            params["domains"] = list(domains)
        if tier is not None:
            conditions.append("d.tier = %(tier)s")
            params["tier"] = tier
        if status is not None:
            conditions.append("d.status = ANY(%(status)s)")
            params["status"] = list(status)
        where_sql = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT d.* FROM {schema}.decisions d{where_sql} ORDER BY d.decision_id"

        async with self._read_conn() as conn:
            cur = await conn.execute(sql, params)
            decision_rows = await cur.fetchall()
            decisions = await self._hydrate_decisions(conn, decision_rows)
            ids = [r["decision_id"] for r in decision_rows]

            links: list[Link] = []
            if ids:
                cur = await conn.execute(
                    f"SELECT from_id, to_id, kind FROM {schema}.links "
                    "WHERE from_id = ANY(%s) OR to_id = ANY(%s)",
                    (ids, ids),
                )
                links = [
                    Link(from_id=r["from_id"], to_id=r["to_id"], kind=r["kind"]) async for r in cur
                ]

            transitions: list[Transition] = []
            if ids:
                cur = await conn.execute(
                    f"SELECT * FROM {schema}.transitions WHERE decision_id = ANY(%s) "
                    "ORDER BY at ASC, transition_id ASC",
                    (ids,),
                )
                transitions = [_row_to_transition(r) async for r in cur]

        domain_records = await self.list_domains()

        return ExportBundle(
            schema_version=_EXPORT_SCHEMA_VERSION,
            decisions=decisions,
            links=links,
            transitions=transitions,
            domains=domain_records,
        )


if TYPE_CHECKING:
    # Static-only check that PostgresStore actually satisfies StorePort — never
    # constructed at runtime, just gives mypy a chance to catch signature drift
    # between the two (§8: extension points are explicit interfaces).
    _store_port_check: StorePort = PostgresStore(dsn="")
