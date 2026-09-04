# Component: Store and Migrations

## Purpose

Durable persistence for the decision record (REQUIREMENTS NFR-1/4/5/7;
ARCHITECTURE §4). The single place binnacle touches PostgreSQL. It stores and
retrieves; every business rule lives in the Lifecycle Engine above it, and every
storage invariant below exists to make a stated rule impossible to violate by
accident.

## Owns

The `binnacle` schema (name configurable): `decisions`, `links`, `refs`,
`transitions`, `queue`, `domains`, `embeddings` — DDL authoritative in
ARCHITECTURE §4 — plus the yoyo migration set, the partial indexes, and the
transactional write primitives the Lifecycle Engine composes.

## Depends on

psycopg3 (async) + pgvector adapter; yoyo-migrations. Implements the store port
declared in `application`; `domain` imports neither.

## Storage decisions

| Decision | Choice | Why |
|---|---|---|
| Driver | psycopg3 async, one pool | ARCHITECTURE P-1; parameterized SQL only. |
| Schema namespace | everything under `schema_name` (default `binnacle`) | Ownership boundary in a shared DB (§4.1); embedder provisions DB/role/extension. |
| Migrations | yoyo-migrations, programmatic API, apply + rollback steps where physically possible | User ruling: incident rollback path comes for free; yoyo bookkeeping tables accepted wherever yoyo puts them. Host invokes `migrate()`; never implicit. |
| Timestamps | TIMESTAMPTZ, UTC | One clock. |
| Ids | UUID (caller-supplied or minted, FR-1.6) | Idempotent recording. |
| Vector column | `VECTOR(768)` fixed at migration (nomic, OQ-3) | Dimension changes are migrations, deliberately. |
| Hot-path indexes | partial, `status NOT IN ('archived','discarded')` | NFR-5/7: performance binds to the active working set. |
| HNSW index | on `embeddings.embedding` | Precedent k-NN at NFR-7 targets. |

## Behavior contract

- **Write primitives are composable within one caller transaction**: the store
  exposes `async with store.transaction() as tx:` and every mutation
  (insert_decision, insert_link, insert_transition, set_status, queue ops,
  upsert_embedding) takes `tx`. The Lifecycle Engine owns composition (I-1);
  the store never commits on its own inside a verb.
- **Idempotent insert** (FR-1.6): `insert_decision` with an existing id compares
  a content hash — identical returns the existing row untouched; different raises
  `IdempotencyConflict`. Never an UPDATE.
- **Concurrency discipline (I-1):** every lifecycle act's transaction opens with
  `SELECT … FOR UPDATE` on all touched decision rows; queue resolution is a
  guarded `UPDATE … SET resolved = TRUE WHERE item_id = $1 AND NOT resolved`
  whose row count must be 1 (double-resolution impossible).
- **Status is written only alongside its transition** — the store offers a single
  `apply_transition(tx, decision_id, action, actor, reason, payload,
  new_status | None)` that appends the transition (recording `new_status` IN the
  transition row — the computable fold, I-1) and updates the denormalized status
  in one statement pair; there is no bare `set_status`. Registry changes go
  through the parallel `domain_transitions` audit table.
- **Content columns have no UPDATE path at all** (I-3) — the store simply defines
  no method that can touch them post-insert; the perf/regression suite asserts
  this by API inventory.
- **Reads**: relevance (subject-or-unscoped OR-join per FR-6.1, lexical filter
  via ILIKE/tsquery — plan decides which), history (decision + transitions +
  links + recursive-CTE chains), changes feed (indexed transition scans),
  queue reads, candidate enumerations (FR-6.9: unembedded backlog, aging
  unrecommended, k-NN neighbor lookups), projections (compact selects only the
  compact columns — no fetch-then-trim).
- **Ref/link integrity**: FK-enforced; `links` inserts validate kind against the
  closed set; supersession chains acyclic (checked in Lifecycle Engine via chain
  walk, not a DB constraint — documented).

## Migrations layout

```
src/binnacle/migrations/
  0001_schema.sql          # + 0001_schema.rollback.sql
  0002_indexes.sql         # + rollback
  ...
```

`Binnacle.migrate()` runs yoyo programmatically against the configured DSN/pool,
scoped to the migration package; preflight checks: pgvector extension present
(error with provisioning hint if not), schema creatable, and — post-migration
and at client construction — `config.embedding_dim` equals the actual
`VECTOR(n)` column typmod (`EmbeddingDimensionMismatch` otherwise, killing the
poison-backlog failure class at the door).

## Acceptance

- Round-trip property tests per table; idempotent-insert tests (identical no-op,
  divergent `IdempotencyConflict`); a concurrency race test (parallel
  promote/supersede on one decision — one wins, one gets `InvalidTransition`,
  status equals fold; double-tap queue resolution resolves once); the
  apply_transition pairing test (status never diverges from transition fold —
  seeded random walks over the state machine, then `status == fold(transitions)`
  asserted for every decision).
- Migration cycle test: apply all → rollback last → re-apply, against a scratch
  schema.
- NFR-7 perf seed test: 10k decisions / 100k transitions; each target's query
  measured under its bound (generous CI multiplier, house pattern).
- Two-schema coexistence test (two configs, one database).
