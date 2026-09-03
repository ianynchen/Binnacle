"""Integration tests for PostgresStore.migrate() (needs a live Postgres; see conftest.pg_dsn).

Each test gets its own scratch schema (tests/conftest.py's scratch_schema fixture),
dropped afterwards — migrations create real objects in a real database.
"""

import tempfile
import uuid as uuid_module
from pathlib import Path

import psycopg
import pytest
from yoyo import get_backend, read_migrations

from binnacle.adapters.postgres_store import PostgresStore, _render_migrations, _yoyo_uri
from binnacle.domain.errors import EmbeddingDimensionMismatch

EXPECTED_TABLES = {
    "domains",
    "decisions",
    "links",
    "refs",
    "transitions",
    "queue",
    "embeddings",
    "domain_transitions",
}


def _tables_in(pg_dsn: str, schema: str) -> set[str]:
    with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (schema,),
        )
        return {row[0] for row in cur.fetchall()}


def _index_exists(pg_dsn: str, schema: str, index_name: str) -> bool:
    with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
            (schema, index_name),
        )
        return cur.fetchone() is not None


async def test_migrate_creates_all_tables(pg_dsn: str, scratch_schema: str) -> None:
    """migrate() creates every table named in ARCHITECTURE.md §4, in the target schema only."""
    store = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=768)
    await store.migrate()
    assert EXPECTED_TABLES <= _tables_in(pg_dsn, scratch_schema)


async def test_migrate_accepts_matching_embedding_dim(pg_dsn: str, scratch_schema: str) -> None:
    """A store configured with the dimension the schema was migrated at passes silently."""
    store = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=768)
    await store.migrate()
    await store.migrate()  # idempotent re-run: to_apply() is empty, postflight still checked


async def test_migrate_dimension_mismatch_raises(pg_dsn: str, scratch_schema: str) -> None:
    """EmbeddingDimensionMismatch protects against a poison backlog (VECTOR(n) fixed at
    migration time, per §4.1 / P-2) — configuring a different embedding_dim against an
    already-migrated schema must fail loudly, not silently degrade precedent recall."""
    baseline = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=768)
    await baseline.migrate()  # bakes VECTOR(768) into embeddings.embedding

    mismatched = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=384)
    with pytest.raises(EmbeddingDimensionMismatch):
        await mismatched.migrate()


async def test_apply_rollback_last_reapply(pg_dsn: str, scratch_schema: str) -> None:
    """Migration cycle test (docs/components/02-store-and-migrations.md Acceptance):
    apply all -> rollback the most recently applied migration -> re-apply. Drives yoyo
    directly (the schema already exists via store.migrate()'s preflight, which is the
    only reason PostgresStore.migrate() itself needs to run first)."""
    store = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=768)
    await store.migrate()
    assert _index_exists(pg_dsn, scratch_schema, "idx_queue_dedup")

    with tempfile.TemporaryDirectory() as tmp:
        _render_migrations(Path(tmp), scratch_schema, 768)
        backend = get_backend(_yoyo_uri(pg_dsn, scratch_schema))
        try:
            migrations = read_migrations(tmp)
            last_applied = backend.to_rollback(migrations)[:1]
            assert len(last_applied) == 1

            backend.rollback_migrations(last_applied)
            assert not _index_exists(pg_dsn, scratch_schema, "idx_queue_dedup")
            # 0001's tables are untouched by rolling back only 0002.
            assert EXPECTED_TABLES <= _tables_in(pg_dsn, scratch_schema)

            backend.apply_migrations(backend.to_apply(migrations))
            assert _index_exists(pg_dsn, scratch_schema, "idx_queue_dedup")
        finally:
            backend.connection.close()


async def test_two_schemas_coexist(pg_dsn: str, scratch_schema: str) -> None:
    """Two-schema coexistence test (Acceptance): migrating two schemas in one database
    must not have one schema's migration bookkeeping mask the other's (see
    adapters.postgres_store module docstring on the yoyo `schema` URI param)."""
    other_schema = f"bt_{uuid_module.uuid4().hex[:12]}"
    try:
        store_a = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=768)
        store_b = PostgresStore(dsn=pg_dsn, schema_name=other_schema, embedding_dim=768)
        await store_a.migrate()
        await store_b.migrate()

        assert EXPECTED_TABLES <= _tables_in(pg_dsn, scratch_schema)
        assert EXPECTED_TABLES <= _tables_in(pg_dsn, other_schema)
    finally:
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{other_schema}" CASCADE')
