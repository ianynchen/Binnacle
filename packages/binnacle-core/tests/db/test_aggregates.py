"""Integration tests for `relevant_count()` (needs a live Postgres, see
conftest.pg_dsn). Mirrors the `bn` fixture / `_nd` builder pattern from
tests/db/test_client.py -- fixtures are per-module in this repo (test_query.py
and test_client.py each define their own), so this module does too.
"""

from collections.abc import AsyncIterator

import pytest

from binnacle_core.application.config import BinnacleConfig
from binnacle_core.client import Binnacle
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
    await client.add_domain("product", "product decisions", actor=HUMAN)
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


class TestRelevantCount:
    async def test_count_reflects_the_same_filters_as_relevant(self, bn: Binnacle) -> None:
        """A count that ignored a filter would overstate what the caller can
        actually page through."""
        await bn.record(_nd(), actor=AGENT)
        await bn.record(_nd(scenario="cache eviction policy for hot keys"), actor=AGENT)
        await bn.record(_nd(domain="product", scenario="pricing tier rollout"), actor=AGENT)

        in_arch = await bn.relevant(domains=["architecture"], limit=1000)
        assert await bn.relevant_count(domains=["architecture"]) == len(in_arch.items)
        assert await bn.relevant_count(domains=["nonexistent"]) == 0


class TestSummaries:
    async def test_queue_summary_counts_open_items_by_kind(self, bn: Binnacle) -> None:
        d = await bn.record(_nd(), actor=AGENT)
        await bn.recommend(d.decision_id, actor=AGENT, reason="looks solid")

        summary = await bn.queue_summary()
        open_items = await bn.queue(limit=1000)
        assert sum(summary.values()) == len(open_items.items)

    async def test_queue_summary_ignores_resolved_items(self, bn: Binnacle) -> None:
        """A summary that counted resolved items would misreport the review
        backlog as permanently growing, never shrinking as a human works
        through it."""
        d = await bn.record(_nd(), actor=AGENT)
        await bn.recommend(d.decision_id, actor=AGENT, reason="looks solid")

        before = await bn.queue_summary()
        item = (await bn.queue(limit=1)).items[0]
        await bn.dismiss_item(item.item.item_id, actor=HUMAN, reason="noise")
        after = await bn.queue_summary()
        assert sum(after.values()) == sum(before.values()) - 1

    async def test_domain_summary_includes_domains_with_no_decisions(self, bn: Binnacle) -> None:
        """The registry-housekeeping use case is looking for exactly these
        rows, which a plain GROUP BY over decisions would omit."""
        await bn.record(_nd(), actor=AGENT)
        await bn.add_domain("unused", "nothing recorded here yet", actor=HUMAN)

        summaries = {s.name: s for s in await bn.domain_summary()}
        assert summaries["unused"].decision_count == 0
        assert summaries["architecture"].decision_count > 0
