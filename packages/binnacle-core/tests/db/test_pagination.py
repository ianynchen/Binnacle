"""Keyset pagination over relevant() (needs a live Postgres, see conftest.pg_dsn).
Mirrors the `bn` fixture / `_nd` builder pattern from tests/db/test_client.py --
fixtures are per-module in this repo (test_query.py and test_client.py each
define their own), so this module does too.

The invariant that matters is not "a cursor round-trips" but "paging through
yields each decision exactly once, and as many as the count promised" --
tested here rather than only in the unit codec tests.
"""

from collections.abc import AsyncIterator

import pytest

from binnacle_core.application.config import BinnacleConfig
from binnacle_core.client import Binnacle
from binnacle_core.domain.errors import InvalidCursor
from binnacle_core.domain.models import Actor, NewDecision
from tests.helpers import StubEmbedder

HUMAN = Actor("human", "alice")
AGENT = Actor("agent", "meridian/sess-1")


@pytest.fixture()
async def bn(pg_dsn: str, scratch_schema: str) -> AsyncIterator[Binnacle]:
    config = BinnacleConfig(dsn=pg_dsn, schema_name=scratch_schema, embedder=StubEmbedder(dim=8))
    client = Binnacle(config)
    await client.migrate()
    await client.add_domain("architecture", "architecture decisions", actor=HUMAN)
    yield client
    await client.aclose()


def _nd(**overrides: object) -> NewDecision:
    base: dict[str, object] = {
        "domain": "architecture",
        "scenario": "adopt exponential backoff for retries",
        "outcome": "retries use exponential backoff with jitter",
        "reasoning": "reduces thundering herd under load",
        "source": "test-suite",
    }
    base.update(overrides)
    return NewDecision(**base)  # type: ignore[arg-type]


class TestRelevantPagination:
    async def test_paging_yields_every_decision_exactly_once(self, bn: Binnacle) -> None:
        for i in range(11):
            await bn.record(_nd(scenario=f"decision {i}"), actor=AGENT)

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(50):  # generous bound; asserts termination too
            page = await bn.relevant(limit=3, after=cursor)
            seen.extend(str(d.id) for d in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert cursor is None, "pagination did not terminate"
        assert len(seen) == len(set(seen)), "a decision appeared on two pages"

    async def test_paged_total_equals_relevant_count(self, bn: Binnacle) -> None:
        """The count and the pages must describe the same set -- this is what
        the signature test in tests/unit cannot prove."""
        for i in range(7):
            await bn.record(_nd(scenario=f"decision {i}"), actor=AGENT)

        total = await bn.relevant_count(domains=["architecture"])
        seen = 0
        cursor: str | None = None
        while True:
            page = await bn.relevant(domains=["architecture"], limit=2, after=cursor)
            seen += len(page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == total

    async def test_last_page_reports_no_next_cursor(self, bn: Binnacle) -> None:
        for i in range(3):
            await bn.record(_nd(scenario=f"decision {i}"), actor=AGENT)

        page = await bn.relevant(limit=1000)
        assert page.next_cursor is None

    async def test_replaying_a_cursor_under_a_different_sort_is_refused(self, bn: Binnacle) -> None:
        await bn.record(_nd(scenario="decision a"), actor=AGENT)
        await bn.record(_nd(scenario="decision b"), actor=AGENT)

        page = await bn.relevant(sort="recorded_at", order="desc", limit=1)
        assert page.next_cursor is not None
        with pytest.raises(InvalidCursor):
            await bn.relevant(sort="decided_at", order="desc", after=page.next_cursor)
