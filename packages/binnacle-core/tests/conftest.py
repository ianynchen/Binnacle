import os
import uuid

import psycopg
import pytest

DSN = os.environ.get("BINNACLE_TEST_DSN", "postgresql://localhost:5432/binnacle_test")


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            pass
    except Exception as exc:  # noqa: BLE001 - any connection failure means "skip"
        pytest.skip(f"postgres unreachable at {DSN}: {exc}")
    return DSN


@pytest.fixture()
def scratch_schema(pg_dsn: str) -> str:  # yields a unique schema name; drops it after
    name = f"bt_{uuid.uuid4().hex[:12]}"
    yield name
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
