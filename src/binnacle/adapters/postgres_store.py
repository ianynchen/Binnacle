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
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
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
    EmbeddingDimensionMismatch,
    IdempotencyConflict,
    ItemAlreadyResolved,
    ItemNotFound,
)
from binnacle.domain.models import Actor, Decision, LinkKind, QueueItem, QueueKind, Ref

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_QUEUE_DEDUP_TARGET = (
    "(kind, decision_id, COALESCE(target_id, '00000000-0000-0000-0000-000000000000'::uuid))"
    " WHERE NOT resolved"
)


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
            ConfigError: both or neither of `dsn`/`pool` were supplied.
        """
        if (dsn is None) == (pool is None):
            msg = "PostgresStore requires exactly one of dsn or pool"
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


if TYPE_CHECKING:
    # Static-only check that PostgresStore actually satisfies StorePort — never
    # constructed at runtime, just gives mypy a chance to catch signature drift
    # between the two (§8: extension points are explicit interfaces).
    _store_port_check: StorePort = PostgresStore(dsn="")
