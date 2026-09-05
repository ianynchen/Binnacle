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
