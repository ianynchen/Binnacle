"""Keyset pagination over relevant() (needs a live Postgres, see conftest.pg_dsn).
Mirrors the `bn` fixture / `_nd` builder pattern from tests/db/test_client.py --
fixtures are per-module in this repo (test_query.py and test_client.py each
define their own), so this module does too.

The invariant that matters is not "a cursor round-trips" but "paging through
yields each decision exactly once, and as many as the count promised" --
tested here rather than only in the unit codec tests.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

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

    async def test_ascending_paging_returns_genuinely_ascending_order(self, bn: Binnacle) -> None:
        """Every other test in this module pages in the default `order="desc"`.
        `_relevant_keyset` picks its comparison operator with
        `"<" if order == "desc" else ">"` -- a flipped operator on the
        ascending branch would still return each decision exactly once (just
        on the wrong page), so uniqueness alone can't catch it. Seed decisions
        with known, strictly increasing `decided_at` values and assert the
        concatenated page order matches that sequence exactly."""
        base = datetime(2024, 1, 1, tzinfo=UTC)
        ids: list[UUID] = []
        for i in range(11):
            decision = await bn.record(
                _nd(scenario=f"decision {i}", decided_at=base + timedelta(days=i)), actor=AGENT
            )
            ids.append(decision.decision_id)

        seen: list[UUID] = []
        cursor: str | None = None
        for _ in range(50):  # generous bound; asserts termination too
            page = await bn.relevant(sort="decided_at", order="asc", limit=3, after=cursor)
            seen.extend(d.id for d in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert cursor is None, "pagination did not terminate"
        assert len(seen) == len(set(seen)), "a decision appeared on two pages"
        assert seen == ids, "pages were not in genuinely ascending decided_at order"

    async def test_last_touched_at_paging_with_full_projection(self, bn: Binnacle) -> None:
        """`last_touched_at` is the one sort key whose value comes from a
        `LEFT JOIN LATERAL` over `transitions` rather than a stored column,
        and `projection="full"` is the `SELECT d.*, {expr} AS _sort_value`
        path -- neither is exercised by any other pagination test. Page
        through both together and check the same once-each/terminates
        properties the other tests check."""
        for i in range(11):
            await bn.record(_nd(scenario=f"decision {i}"), actor=AGENT)

        seen: list[UUID] = []
        cursor: str | None = None
        for _ in range(50):  # generous bound; asserts termination too
            page = await bn.relevant(
                projection="full", sort="last_touched_at", limit=3, after=cursor
            )
            seen.extend(d.decision_id for d in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert cursor is None, "pagination did not terminate"
        assert len(seen) == len(set(seen)), "a decision appeared on two pages"


class TestQueuePagination:
    async def test_paging_the_queue_yields_each_item_once(self, bn: Binnacle) -> None:
        for i in range(11):
            source = await bn.record(_nd(scenario=f"decision {i}"), actor=AGENT)
            await bn.recommend(source.decision_id, actor=AGENT, reason="ready")

        seen: list[int] = []
        cursor: str | None = None
        for _ in range(50):  # generous bound; asserts termination too
            page = await bn.queue(limit=2, after=cursor)
            seen.extend(v.item.item_id for v in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert cursor is None, "pagination did not terminate"
        assert len(seen) == len(set(seen)), "a queue item appeared on two pages"

    async def test_a_queue_cursor_is_refused_under_a_different_order(self, bn: Binnacle) -> None:
        for i in range(3):
            source = await bn.record(_nd(scenario=f"decision {i}"), actor=AGENT)
            await bn.recommend(source.decision_id, actor=AGENT, reason="ready")

        page = await bn.queue(order="oldest", limit=1)
        assert page.next_cursor is not None
        with pytest.raises(InvalidCursor):
            await bn.queue(order="shakiest", after=page.next_cursor)

    async def test_paging_the_queue_under_domain_order_yields_each_item_once(
        self, bn: Binnacle
    ) -> None:
        """`order="domain"` leads with a domain-name string rather than a
        datetime or number -- before the cursor codec's `vt` tag existed,
        `decode_cursor` tried `datetime.fromisoformat()` on every string `v`,
        so any `after` cursor minted under this order raised `InvalidCursor`
        on the very next page. Paging past the first page proves the cursor
        this order mints is actually replayable."""
        for i in range(11):
            source = await bn.record(_nd(scenario=f"decision {i}"), actor=AGENT)
            await bn.recommend(source.decision_id, actor=AGENT, reason="ready")

        seen: list[int] = []
        cursor: str | None = None
        for _ in range(50):  # generous bound; asserts termination too
            page = await bn.queue(order="domain", limit=2, after=cursor)
            seen.extend(v.item.item_id for v in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert cursor is None, "pagination did not terminate"
        assert len(seen) == len(set(seen)), "a queue item appeared on two pages"
