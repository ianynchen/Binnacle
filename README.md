# Binnacle

A PostgreSQL-backed decision-record library: the fleet's decision record and
precedent engine. Binnacle stores decisions (scenario / outcome / reasoning),
tracks their lifecycle (record → recommend → promote → supersede/supplement →
archive) under a human-gated write path, and answers precedent queries over
pgvector embeddings. It is a library, not a service — no daemon, no env/file
reads, no authorization logic (see "Actor attestation" below). See
[`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full contract.

## Install

```bash
uv add "binnacle @ git+https://github.com/ianynchen/Binnacle.git"
# or
pip install "binnacle @ git+https://github.com/ianynchen/Binnacle.git"
```

Requires Python ≥3.13 and PostgreSQL 18 with the `pgvector` extension
available.

## Provisioning preconditions

Binnacle ships schema migrations (`Binnacle.migrate()`) but does **not**
provision the database, role, or extension it runs in — that is the host's
job, performed once by a privileged role before the library is ever used:

```sql
-- as a privileged role (e.g. postgres superuser)
CREATE DATABASE my_app;
\c my_app
CREATE EXTENSION vector;
```

Binnacle then owns and migrates only its own schema inside that database
(default name `binnacle`, configurable via `BinnacleConfig.schema_name`) —
it does not touch tables outside that schema, and does not need superuser
rights itself, only `CREATE`/`USAGE` on its schema. `migrate()` checks for
the `vector` extension and raises `ConfigError` (never attempts to create it
itself) if it is missing.

## Quickstart

```python
import asyncio
from binnacle import Actor, Binnacle, BinnacleConfig, NewDecision


class DemoEmbedder:  # stand-in for a real Embedder (e.g. nomic-embed-text-v1.5)
    async def embed(self, texts: list[str]) -> list[list[float]]:  # zero vectors, illustrative only
        return [[0.0] * 768 for _ in texts]


async def main() -> None:
    config = BinnacleConfig(
        dsn="postgresql://localhost:5432/binnacle_test", embedder=DemoEmbedder()
    )
    bn = Binnacle(config)
    await bn.migrate()

    human, agent = Actor("human", "alice"), Actor("agent", "meridian/sess-1")
    await bn.add_domain("architecture", "system design decisions", actor=human)

    nd = NewDecision(
        domain="architecture",
        scenario="how to handle transient ingestion failures?",
        outcome="retry with exponential backoff, capped at 3 attempts",
        reasoning="avoids thundering herd on recovery",
        source="meridian",
    )
    decision = await bn.record(nd, actor=agent)
    await bn.recommend(decision.decision_id, actor=agent, reason="stable after a week")
    await bn.promote_refined([decision.decision_id], refined=nd, actor=human)

    for hit in await bn.precedent("how do we handle flaky network calls?"):
        print(hit.decision.outcome_truncated, hit.similarity)


asyncio.run(main())
```

`DemoEmbedder` above is illustrative only (it returns zero vectors, so
`precedent()` similarity scores are meaningless) — production callers supply
a real `Embedder` (meridian fulfills it via `nomic-embed-text-v1.5`; tests
use the deterministic `StubEmbedder` in `tests/helpers.py`, not shipped in
the package).

## The guardrail stack

- **`pre-commit`** (`.pre-commit-config.yaml`): `gitleaks` (hardcoded-secret
  scanning) and `ruff` (lint + format), run on every commit —
  `pre-commit install` once, then `pre-commit run --all-files` to check
  everything.
- **CI** (`.github/workflows/ci.yml`, GitHub Actions): on every push/PR to
  `main`, spins up a `pgvector/pgvector:pg18` Postgres service, runs
  `scripts/check.sh` (ruff format/lint, mypy strict, import-linter,
  `pytest`) and `pre-commit run --all-files`.
- **import-linter** (`pyproject.toml` `[tool.importlinter]`): enforces the
  layering `binnacle.adapters → binnacle.application → binnacle.domain`,
  and that `domain`/`application` stay free of DB-driver imports
  (`psycopg`, `yoyo`, `pgvector`).

Run everything locally with `bash scripts/check.sh`.

## Running the tests

Integration tests need a live Postgres with `pgvector` installed. They read
`BINNACLE_TEST_DSN` (default `postgresql://localhost:5432/binnacle_test`)
and **skip cleanly** (`pytest.skip`) when that DSN is unreachable, so
`pytest`/`scripts/check.sh` runs anywhere — unit-only when no database is
reachable, the full suite when one is.

```bash
createdb binnacle_test
psql binnacle_test -c "CREATE EXTENSION vector"
export BINNACLE_TEST_DSN=postgresql://localhost:5432/binnacle_test
uv run pytest
```

## Actor attestation

Every write-path verb takes an explicit `Actor(kind, id)` — binnacle **never
guesses or infers** an actor's kind. The caller (meridian) authenticates its
users/agents and attests `kind ∈ {human, agent, engine}` at the call site;
binnacle enforces the authority rule (e.g. "promotion requires a human") only
against the attested `kind`, and records `id` as given. Id honesty *within*
a kind (e.g. one agent claiming another agent's id) is the caller's
enforcement duty, not binnacle's — this is attribution, not authorization
(FR-8.2, ARCHITECTURE I-2/DR-5).

## Embedding text convention

The text embedded for a decision (both at backfill and at discovery
re-embedding time) is always:

```python
"\n\n".join([decision.scenario, decision.outcome, decision.reasoning])
```

fulfilled by `nomic-embed-text-v1.5` (768 dimensions, 8192-token context —
OQ-3), matching `BinnacleConfig.embedding_dim`'s default of `768`. A caller
supplying a custom `Embedder` must embed the same convention for `precedent()`
similarity scores to mean anything, and must set `embedding_dim` to match its
own vector width.

## Limitations / known v1 gaps

Binnacle v1 deliberately leaves the following out of scope; see
[`docs/REQUIREMENTS.md` §5](docs/REQUIREMENTS.md) for the full v2 list and
named adoption triggers:

- **No import path.** `export()` produces a JSON bundle; there is no
  corresponding `import` to load one back into a store (REQUIREMENTS §5).
- **Precedent over-fetch is a fixed multiplier, not adaptive.**
  `precedent()` over-fetches candidates by a fixed `4×` factor when
  `domains`/`tiers`/`include_dead=False` will drop some (matching
  `store.knn`'s own over-fetch factor) — a filter that rejects more than 3
  of 4 candidates can still return fewer than `limit` results
  (`src/binnacle/application/query.py`).
- **Discovery re-embeds rather than reading back stored vectors.** The
  `StorePort` has no "read one embedding back" primitive, so `discover()`
  re-derives a subject decision's vector by re-embedding its text for its
  own k-NN lookup, rather than reading the vector already stored by
  `backfill_embeddings()` (`src/binnacle/application/discovery.py`).
- **Reversed-pair suggestions on equal `recorded_at`.** Discovery's temporal
  filter allows `other.recorded_at <= subject.recorded_at`; when two
  decisions share the exact same `recorded_at`, each can appear as the
  other's "later" side during its own discovery pass, potentially enqueuing
  the relationship suggestion in both directions (the dedup index keys on
  `(kind, decision_id, target_id)`, which does not catch a reversed pair).
- **No conflict detection or relationship taxonomy beyond
  supersedes/supplements/unrelated.** Automated conflict detection across
  current decisions and a richer relationship taxonomy remain v2
  (REQUIREMENTS FR-7.2, §5).
