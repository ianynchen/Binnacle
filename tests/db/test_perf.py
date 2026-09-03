"""Perf seed test (REQUIREMENTS NFR-7; needs a live Postgres, see
conftest.pg_dsn). Seeds the design-scale dataset NFR-7 itself names --
10,000 decisions / 100,000 transitions -- via raw `COPY` directly into a
scratch schema (bulk fixture setup, deliberately bypassing the Lifecycle
Engine: this is data to query against, not a sequence of lifecycle acts under
test -- those are covered exhaustively elsewhere), then measures each NFR-7
row's own operation through the PUBLIC `Binnacle` client (or, for the one row
NFR-7 itself calls "store-side", `PostgresStore.knn` directly) over several
iterations and asserts its 95th-percentile latency against
`target_p95 * CI_MULTIPLIER` -- REQUIREMENTS NFR-7's own "generous CI bound
over the measured local number" house pattern, with the multiplier this
task's brief settled on (4x).

Seed data is structurally valid (every FK resolves, every column respects its
NOT NULL/enum constraints) but NOT semantically fold-consistent (a decision's
`transitions` rows don't necessarily reduce to its own `status` the way I-1
requires of a REAL lifecycle history) -- irrelevant here: these rows exist to
be *read* at scale, and every lifecycle invariant they'd otherwise exercise
already has dedicated coverage in tests/db/test_lifecycle.py.

`@pytest.mark.perf` is registered in pyproject.toml and left in the default
`pytest`/`check.sh` run -- see the marker's own registration comment there
for the measured wall-clock rationale.
"""

import math
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from binnacle.adapters.postgres_store import PostgresStore
from binnacle.application.config import BinnacleConfig
from binnacle.client import Binnacle
from binnacle.domain.models import Actor, NewDecision
from tests.helpers import StubEmbedder

pytestmark = pytest.mark.perf

HUMAN = Actor("human", "alice")
DIM = 32

N_DECISIONS = 10_000
TRANSITIONS_PER_DECISION = 10  # 10,000 * 10 = 100,000 -- NFR-7's own numbers.
N_PROMOTION_TARGETS = 20  # decisions with a dedicated open 'promote' item.
N_QUEUE_FILLER = 480  # padding so "queue read" sees a realistic open-queue size.
ITERATIONS = 20
CI_MULTIPLIER = 4

# NFR-7 p95 targets (REQUIREMENTS.md NFR-7 table), in seconds.
_TARGET_SECONDS = {
    "record a decision": 0.250,
    "relevance query (FR-6.1)": 0.200,
    "single-decision history (FR-6.2)": 0.100,
    "precedent search, store-side (FR-6.3)": 0.500,
    "queue read (FR-6.4)": 0.200,
    "changes feed (FR-6.5)": 0.200,
    "promotion (copy + edges + transitions)": 0.500,
}

# Realistic short-term status mix for the bulk of the seeded decisions
# (14/20 current, 3/20 superseded, 1/20 each promoted/discarded/archived) --
# exercises `idx_dec_active`'s partial-index exclusion the way a real working
# set would, rather than an all-`current` table.
_STATUS_CYCLE = ["current"] * 14 + ["superseded"] * 3 + ["promoted", "discarded", "archived"]
_TRANSITION_ACTIONS = [
    "recorded",
    "recommended",
    "declined",
    "recommended",
    "supplement_linked",
    "reactivated",
    "recommended",
    "voided",
    "dismissed",
    "recommended",
]


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[idx]


async def _measure(op: Callable[[], Awaitable[object]], iterations: int) -> list[float]:
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        await op()
        samples.append(time.perf_counter() - start)
    return samples


def _decision_rows(
    all_ids: list[uuid.UUID], n_reserved: int, now: datetime
) -> Iterator[tuple[Any, ...]]:
    """`all_ids[:n_reserved]` are the promotion-target + queue-filler
    decisions (module-level docstring): forced short-term/current so every
    one is a legal `promote()` target. Everything after that follows
    `_STATUS_CYCLE` for a realistic mixed working set."""
    for i, decision_id in enumerate(all_ids):
        is_reserved = i < n_reserved
        tier = "short_term" if is_reserved or i % 10 != 0 else "long_term"
        status = "current" if is_reserved else _STATUS_CYCLE[i % len(_STATUS_CYCLE)]
        if tier == "long_term" and status not in ("current", "superseded"):
            status = "current"
        recorded_at = now - timedelta(days=i % 180, minutes=i % 1440)
        yield (
            decision_id,
            tier,
            "perf",
            status,
            f"scenario {i}",
            f"outcome {i}",
            f"reasoning {i}",
            Jsonb([]),
            None,
            None,
            "perf-seed",
            f"hash-{i}",
            "engine:binnacle",
            recorded_at,
            recorded_at,
            None,
            None,
            Jsonb({}),
            1,
        )


def _transition_rows(
    decision_ids: list[uuid.UUID], per_decision: int, now: datetime
) -> Iterator[tuple[Any, ...]]:
    for d_idx, decision_id in enumerate(decision_ids):
        base_at = now - timedelta(days=d_idx % 180)
        for t_idx in range(per_decision):
            action = _TRANSITION_ACTIONS[t_idx % len(_TRANSITION_ACTIONS)]
            new_status = "current" if t_idx == 0 else None
            yield (
                decision_id,
                action,
                "engine:binnacle",
                base_at + timedelta(minutes=t_idx),
                None,
                new_status,
                None,
            )


def _embedding_rows(
    decision_ids: list[uuid.UUID], vectors: list[list[float]], now: datetime
) -> Iterator[tuple[Any, ...]]:
    for decision_id, vector in zip(decision_ids, vectors, strict=True):
        text = "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
        yield (decision_id, text, now, None)


def _queue_rows(decision_ids: Iterable[uuid.UUID], now: datetime) -> Iterator[tuple[Any, ...]]:
    for decision_id in decision_ids:
        yield (
            "promote",
            decision_id,
            None,
            "agent:meridian/s1",
            now,
            "aging, worth a look",
            0.7,
            False,
        )


def _seed(
    pg_dsn: str,
    schema: str,
    now: datetime,
    all_ids: list[uuid.UUID],
    vectors: list[list[float]],
    reserved_ids: list[uuid.UUID],
    promotion_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Bulk-load the design-scale dataset via `COPY`, then look up the
    `promote` queue item id filed for each of `promotion_ids`. Returns that
    `decision_id -> item_id` map -- the only piece the measurement phase
    needs back, since `COPY` itself never returns generated ids."""
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        with (
            conn.cursor() as cur,
            cur.copy(
                f"COPY {schema}.decisions ("
                "  decision_id, tier, domain, status, scenario, outcome, reasoning,"
                "  options_considered, consequences, confidence, source, content_hash,"
                "  recorded_by, decided_at, recorded_at, valid_from, valid_until,"
                "  metadata, schema_version"
                ") FROM STDIN"
            ) as copy,
        ):
            for row in _decision_rows(all_ids, len(reserved_ids), now):
                copy.write_row(row)

        with (
            conn.cursor() as cur,
            cur.copy(
                f"COPY {schema}.transitions (decision_id, action, actor, at, reason, new_status, payload)"
                " FROM STDIN"
            ) as copy,
        ):
            for row in _transition_rows(all_ids, TRANSITIONS_PER_DECISION, now):
                copy.write_row(row)

        with (
            conn.cursor() as cur,
            cur.copy(
                f"COPY {schema}.embeddings (decision_id, embedding, embedded_at, discovered_at) FROM STDIN"
            ) as copy,
        ):
            for row in _embedding_rows(all_ids, vectors, now):
                copy.write_row(row)

        with (
            conn.cursor() as cur,
            cur.copy(
                f"COPY {schema}.queue"
                " (kind, decision_id, target_id, proposed_by, proposed_at, rationale, confidence, resolved)"
                " FROM STDIN"
            ) as copy,
        ):
            for row in _queue_rows(reserved_ids, now):
                copy.write_row(row)

        conn.execute(f"ANALYZE {schema}.decisions")
        conn.execute(f"ANALYZE {schema}.transitions")
        conn.execute(f"ANALYZE {schema}.embeddings")
        conn.execute(f"ANALYZE {schema}.queue")

        rows = conn.execute(
            f'SELECT item_id, decision_id FROM "{schema}".queue '
            "WHERE kind = 'promote' AND decision_id = ANY(%s)",
            (promotion_ids,),
        ).fetchall()
        return {decision_id: item_id for item_id, decision_id in rows}


class TestSeededPerf:
    async def test_nfr7_targets_at_design_scale(self, pg_dsn: str, scratch_schema: str) -> None:
        embedder = StubEmbedder(dim=DIM)
        config = BinnacleConfig(
            dsn=pg_dsn, schema_name=scratch_schema, embedder=embedder, embedding_dim=DIM
        )
        bn = Binnacle(config)
        await bn.migrate()
        await bn.add_domain("perf", "perf-seeded decisions", actor=HUMAN)

        now = datetime.now(UTC)
        promotion_ids = [uuid.uuid4() for _ in range(N_PROMOTION_TARGETS)]
        filler_ids = [uuid.uuid4() for _ in range(N_QUEUE_FILLER)]
        reserved_ids = promotion_ids + filler_ids
        all_ids = reserved_ids + [uuid.uuid4() for _ in range(N_DECISIONS - len(reserved_ids))]
        vectors = await embedder.embed([str(d) for d in all_ids])
        history_sample_ids = all_ids[5000 : 5000 + ITERATIONS]

        item_by_decision = _seed(
            pg_dsn, scratch_schema, now, all_ids, vectors, reserved_ids, promotion_ids
        )

        store = PostgresStore(dsn=pg_dsn, schema_name=scratch_schema, embedding_dim=DIM)
        results: dict[str, float] = {}
        try:
            history_targets = iter(history_sample_ids)
            results["single-decision history (FR-6.2)"] = _p95(
                await _measure(lambda: bn.history(next(history_targets)), ITERATIONS)
            )

            results["relevance query (FR-6.1)"] = _p95(
                await _measure(lambda: bn.relevant(domains=["perf"], limit=50), ITERATIONS)
            )

            results["changes feed (FR-6.5)"] = _p95(
                await _measure(lambda: bn.changes(actions=["recommended"]), ITERATIONS)
            )

            results["queue read (FR-6.4)"] = _p95(
                await _measure(lambda: bn.queue(kinds=["promote"]), ITERATIONS)
            )

            [query_vector] = await embedder.embed(["how should retry backoff be configured?"])
            results["precedent search, store-side (FR-6.3)"] = _p95(
                await _measure(lambda: store.knn(query_vector, 10), ITERATIONS)
            )

            record_counter = iter(range(ITERATIONS))
            results["record a decision"] = _p95(
                await _measure(
                    lambda: bn.record(
                        NewDecision(
                            domain="perf",
                            scenario=f"perf record scenario {next(record_counter)}",
                            outcome="outcome",
                            reasoning="reasoning",
                            source="perf-test",
                        ),
                        actor=HUMAN,
                    ),
                    ITERATIONS,
                )
            )

            promote_items = iter(item_by_decision[d] for d in promotion_ids)
            results["promotion (copy + edges + transitions)"] = _p95(
                await _measure(
                    lambda: bn.promote(next(promote_items), actor=HUMAN), N_PROMOTION_TARGETS
                )
            )
        finally:
            await store.aclose()
            await bn.aclose()

        report_lines = [f"NFR-7 measured p95 (seconds) vs target*{CI_MULTIPLIER}x:"]
        failures = []
        for name, measured in results.items():
            target = _TARGET_SECONDS[name]
            bound = target * CI_MULTIPLIER
            report_lines.append(
                f"  {name}: measured={measured:.4f}s target_p95={target}s bound={bound}s"
            )
            if measured >= bound:
                failures.append(name)
        print("\n".join(report_lines))

        assert not failures, f"NFR-7 targets exceeded ({CI_MULTIPLIER}x bound): {failures}"
