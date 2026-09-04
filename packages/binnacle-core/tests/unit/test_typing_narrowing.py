"""Static-only guard for `relevant()`'s `@overload` narrowing (owner-requested
Fix 2: `Binnacle.relevant`, `StorePort.relevant`, `PostgresStore.relevant` --
see `packages/binnacle-core/src/binnacle_core/client.py`,
`packages/binnacle-core/src/binnacle_core/application/ports.py`,
`packages/binnacle-core/src/binnacle_core/adapters/postgres_store.py`).

Nothing here runs at test time: none of the functions below are `test_*`
(pytest never collects/calls them, and they take instances this module never
constructs -- a fake `Binnacle`/store isn't worth building just to satisfy an
argument list mypy already checks statically). The actual check is `mypy`'s
pass over this one file -- `scripts/check.sh` runs `mypy src` plus this file
by name (see its comment there): the rest of `tests/` isn't strict-mode
typed and doesn't need to be, so the extra scope stays narrow to the one
module that guards the overloads.

`typing.assert_type` is a runtime no-op (returns its argument unchanged) but
fails `mypy` when the *inferred* type doesn't match exactly -- which is
exactly what proves each `@overload` narrows the call site's return type to
`list[CompactDecision]`/`list[Decision]` instead of falling back to the
implementation's `list[CompactDecision] | list[Decision]` union.
"""

from typing import assert_type

from binnacle_core.adapters.postgres_store import PostgresStore
from binnacle_core.application.ports import StorePort
from binnacle_core.client import Binnacle
from binnacle_core.domain.models import CompactDecision, Decision


async def _client_relevant_narrows(client: Binnacle) -> None:
    default_projection = await client.relevant()
    assert_type(default_projection, list[CompactDecision])

    compact = await client.relevant(projection="compact")
    assert_type(compact, list[CompactDecision])

    full = await client.relevant(projection="full")
    assert_type(full, list[Decision])


async def _store_port_relevant_narrows(store: StorePort) -> None:
    default_projection = await store.relevant()
    assert_type(default_projection, list[CompactDecision])

    compact = await store.relevant(compact_chars=100)
    assert_type(compact, list[CompactDecision])

    full = await store.relevant(compact_chars=None)
    assert_type(full, list[Decision])


async def _postgres_store_relevant_narrows(store: PostgresStore) -> None:
    default_projection = await store.relevant()
    assert_type(default_projection, list[CompactDecision])

    compact = await store.relevant(compact_chars=100)
    assert_type(compact, list[CompactDecision])

    full = await store.relevant(compact_chars=None)
    assert_type(full, list[Decision])
