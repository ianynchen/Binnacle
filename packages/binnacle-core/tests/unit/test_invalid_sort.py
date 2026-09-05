"""`relevant()`'s `sort` validation -- raised in `_relevant_order` before any
I/O (the same choke point both `_relevant_order`'s own `_SORT_EXPRESSIONS`
lookup and `_relevant_keyset`'s later one go through), so this runs without a
live Postgres like tests/unit/test_postgres_store_construction.py.

`sort` is typed `Literal[...]` at the call site, but that's a static-only
guard -- a value that bypassed it (e.g. a REST layer deserializing an
untyped request body straight into this call) must not reach the closed
`_SORT_EXPRESSIONS` dict and raise a bare `KeyError`; it must raise the
library's own `InvalidSort` instead, the same discipline
`InvalidCursor` already applies to a malformed pagination cursor
(tests/unit/test_cursors.py)."""

from typing import cast

import pytest

from binnacle_core.adapters.postgres_store import PostgresStore
from binnacle_core.domain.errors import InvalidSort


async def test_relevant_rejects_an_unknown_sort_key() -> None:
    store = PostgresStore(dsn="postgresql://x")
    with pytest.raises(InvalidSort):
        await store.relevant(sort=cast("str", "not_a_real_sort_key"))  # type: ignore[call-overload]
