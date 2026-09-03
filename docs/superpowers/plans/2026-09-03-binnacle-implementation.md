# Binnacle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Binnacle library — the fleet's decision record and precedent engine — per the committed contract, with guardrails (pre-commit gitleaks+ruff, CI with Postgres, import-linter) from task 1.

**Architecture:** Single-store PostgreSQL (+pgvector) library: relational schema with append-only transitions carrying `new_status` (computable fold), a Lifecycle Engine that is the only writer of status/links/transitions (FOR UPDATE serialization, human gate), queue with structural dedup and auto-void hygiene, cursor-driven discovery, `Suggester`/`Embedder` ports (core LLM-free), yoyo migrations in a dedicated schema namespace.

**Tech Stack:** Python ≥3.13, uv, pydantic v2, psycopg3 (async, pool), pgvector python adapter, yoyo-migrations, pytest (+anyio mode), mypy strict, ruff, import-linter, pre-commit (gitleaks + ruff hooks), GitHub Actions with a pgvector Postgres service.

**Spec:** `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, `docs/components/01..04` — the plan argues from these; executors read the relevant spec sections named in each task.

## Global Constraints

- **Work on `main`** (owner's explicit instruction); push to `origin main` ONLY in Task 10.
- Python `>=3.13`; async-first; pydantic v2; exact pins for psycopg, pgvector, yoyo-migrations (record chosen pins in the task report).
- **Zero LLM/embedding dependencies** (FR-7.1): no semantica, no langchain, no model libs — ports only.
- Layering by import-linter: `binnacle.adapters → binnacle.application → binnacle.domain`; `domain` forbids `psycopg`, `yoyo`, `pgvector`; provider-free everywhere.
- mypy `strict` (per-module overrides only with documented necessity, house precedent); ruff lint + format.
- `scripts/check.sh` = ruff format --check, ruff check, mypy src, lint-imports, pytest — MUST mirror pre-commit so hooks never surprise (owner requirement).
- Tests read `BINNACLE_TEST_DSN` (default `postgresql://localhost:5432/binnacle_test`); DB-touching tests use a session fixture that creates/drops a scratch schema per run and **skip cleanly** (`pytest.skip`) when the DSN is unreachable — check.sh works anywhere, runs fully on the mini and in CI.
- The library itself NEVER reads env/files (FR-8.1) — only tests and CI do.
- Commit style `type: subject`, trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`; every commit passes check.sh AND `pre-commit run --all-files`.
- Actor strings are `kind:id` with kind ∈ human|agent|engine; engine actor is exactly `engine:binnacle`.

## File Structure (locked)

```
src/binnacle/
  domain/        models.py errors.py
  application/   client.py config.py ports.py lifecycle.py recorder.py
                 queue.py query.py discovery.py archival.py export.py
  adapters/      postgres_store.py
  migrations/    0001_schema.sql (+ .rollback.sql) 0002_indexes.sql (+ .rollback.sql)
tests/           unit/… db/… architecture/test_layering.py conftest.py
scripts/check.sh
.pre-commit-config.yaml
.github/workflows/ci.yml
```

---

### Task 1: Scaffold, guardrails, CI

**Files:**
- Create: `pyproject.toml`, `src/binnacle/__init__.py` + subpackage `__init__.py`s, `scripts/check.sh`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `tests/conftest.py`, `tests/architecture/test_layering.py`, `.python-version`, `.gitignore` additions (`.env`, `.venv`)

**Interfaces:**
- Produces: importable `binnacle`; green `scripts/check.sh`; installed pre-commit hook; CI workflow; the `pg_dsn` / `scratch_schema` pytest fixtures every DB task uses.

- [x] **Step 1: pyproject** — hatchling; runtime deps `pydantic>=2.9`, `psycopg[binary,pool]==<current exact>`, `pgvector==<current exact>`, `yoyo-migrations==<current exact>`; dev: pytest, pytest-asyncio (`asyncio_mode="auto"`), mypy, ruff, import-linter, pre-commit. Import-linter:
```toml
[tool.importlinter]
root_package = "binnacle"
[[tool.importlinter.contracts]]
name = "layers"
type = "layers"
layers = ["binnacle.adapters", "binnacle.application", "binnacle.domain"]
[[tool.importlinter.contracts]]
name = "domain is pure"
type = "forbidden"
source_modules = ["binnacle.domain"]
forbidden_modules = ["psycopg", "yoyo", "pgvector"]
[[tool.importlinter.contracts]]
name = "application is driver-free"
type = "forbidden"
source_modules = ["binnacle.application"]
forbidden_modules = ["psycopg", "yoyo"]
```
- [x] **Step 2: `.pre-commit-config.yaml`** (owner requirement — gitleaks + ruff lint + ruff format):
```yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.30.1
    hooks: [{id: gitleaks}]
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: <current ruff-pre-commit tag matching the pinned ruff>
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
```
Run `pre-commit install` and `pre-commit run --all-files`.
- [x] **Step 3: `scripts/check.sh`** — `set -euo pipefail`; `uv run ruff format --check src tests`, `uv run ruff check src tests`, `uv run mypy src`, `uv run lint-imports`, `uv run pytest -q` (mirrors the hooks per Global Constraints).
- [x] **Step 4: `tests/conftest.py`** — the DB fixture contract:
```python
import os, uuid, pytest, psycopg

DSN = os.environ.get("BINNACLE_TEST_DSN", "postgresql://localhost:5432/binnacle_test")


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            pass
    except Exception as exc:
        pytest.skip(f"postgres unreachable at {DSN}: {exc}")
    return DSN


@pytest.fixture()
def scratch_schema(pg_dsn: str) -> str:  # yields a unique schema name; drops it after
    name = f"bt_{uuid.uuid4().hex[:12]}"
    yield name
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
```
(Precondition on the mini: `createdb binnacle_test` and `CREATE EXTENSION vector` in it — do this in this task and record it.)
- [x] **Step 5: `.github/workflows/ci.yml`** — on push/PR to main: checkout; `astral-sh/setup-uv`; services: `postgres: image: pgvector/pgvector:pg18` (fall back to `:pg17` if the tag doesn't exist — verify with `docker manifest inspect` or the registry page and record) with `POSTGRES_DB: binnacle_test`, health-check; env `BINNACLE_TEST_DSN: postgresql://postgres:postgres@localhost:5432/binnacle_test`; run `uv sync` then `bash scripts/check.sh`. Also a `pre-commit run --all-files` step (`pre-commit/action` or uv-run).
- [x] **Step 6: `tests/architecture/test_layering.py`** — subprocess `uv run lint-imports`, assert rc 0 (portable cwd via `Path(__file__).parents[2]`).
- [x] **Step 7: run check.sh + pre-commit → both green; commit** `chore: scaffold binnacle with guardrails (pre-commit, CI, import-linter)`

### Task 2: Domain — models and errors

**Files:**
- Create: `src/binnacle/domain/models.py`, `src/binnacle/domain/errors.py`
- Test: `tests/unit/test_domain_models.py`

**Interfaces (produced — EXACT names all later tasks consume):**
```python
# errors.py
class BinnacleError(Exception): ...


class ConfigError(BinnacleError): ...


class UnknownDomain(BinnacleError): ...


class DecisionNotFound(BinnacleError): ...


class InvalidTransition(BinnacleError): ...  # carries from_status, attempted action


class AuthorityViolation(BinnacleError): ...


class IdempotencyConflict(BinnacleError): ...


class EmbeddingDimensionMismatch(BinnacleError): ...


class ItemNotFound(BinnacleError): ...


class ItemAlreadyResolved(BinnacleError): ...


# models.py (pydantic v2 for validated inputs; frozen dataclasses for records)
ActorKind = Literal["human", "agent", "engine"]
Tier = Literal["short_term", "long_term"]
ShortStatus = Literal["current", "promoted", "not_promoted", "superseded", "discarded", "archived"]
LongStatus = Literal["current", "superseded"]
LinkKind = Literal["SUPERSEDES", "SUPPLEMENTS", "PROMOTED_FROM"]
QueueKind = Literal["promote", "link", "supersede"]
RefRole = Literal["subject", "evidence"]
TransitionAction = Literal[
    "recorded",
    "recommended",
    "promoted",
    "declined",
    "discarded",
    "superseded",
    "supplement_linked",
    "archived",
    "reactivated",
    "voided",
    "dismissed",
]


@dataclass(frozen=True)
class Actor:
    kind: ActorKind
    id: str

    def as_str(self) -> str: ...  # "kind:id"; parse with Actor.from_str()


class Ref(BaseModel):
    role: RefRole
    kind: str
    identifier: str
    note: str | None = None


class NewDecision(BaseModel):  # the recording input (FR-1.1)
    domain: str
    scenario: str
    outcome: str
    reasoning: str
    source: str
    decision_id: UUID | None = None  # FR-1.6
    options_considered: list[OptionConsidered] = []  # OptionConsidered(option, why_rejected)
    consequences: str | None = None
    confidence: float | None = None  # 0..1 validated
    decided_at: datetime | None = None  # FR-1.7
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    refs: list[Ref] = []
    supersedes: list[UUID] = []
    supplements: list[UUID] = []
    metadata: dict[str, Any] = {}

    def content_hash(self) -> str: ...  # sha256 over canonical JSON of content fields (FR-1.6)


@dataclass(frozen=True)
class Decision:
    ...  # full stored record: all NewDecision content + decision_id, tier,
    # status, recorded_by, recorded_at, decided_at, schema_version


@dataclass(frozen=True)
class CompactDecision: ...  # id, domain, tier, status, outcome_truncated, subject_refs (FR-6.7)


@dataclass(frozen=True)
class Transition: ...  # transition_id, decision_id, action, actor, at, reason, new_status, payload


@dataclass(frozen=True)
class Link: ...  # from_id, to_id, kind


@dataclass(frozen=True)
class QueueItem:
    ...  # item_id, kind, decision_id, target_id, proposed_by, proposed_at,
    # rationale, confidence, resolved


@dataclass(frozen=True)
class CandidatePair: ...  # decision: CompactDecision, other: CompactDecision, similarity: float


@dataclass(frozen=True)
class Suggestion: ...  # kind: Literal["supersedes","supplements","unrelated"], rationale, confidence


@dataclass(frozen=True)
class PromotionAssessment: ...  # decision_id, recommend: bool, rationale, confidence
```

- [x] **Step 1: failing tests** — Actor round-trip `"agent:meridian/s1"`; NewDecision confidence bounds (1.5 → ValidationError); `content_hash()` stable under metadata/refs-order changes but differs on outcome change; OptionConsidered required fields.
- [x] **Step 2–5: RED → implement → GREEN + check.sh + pre-commit → commit** `feat(domain): models and errors`

### Task 3: Migrations + store foundation (write side)

**Files:**
- Create: `src/binnacle/migrations/0001_schema.sql` (+rollback), `0002_indexes.sql` (+rollback), `src/binnacle/application/ports.py` (StorePort protocol — transaction + write primitives), `src/binnacle/adapters/postgres_store.py`
- Test: `tests/db/test_migrations.py`, `tests/db/test_store_writes.py`

**Interfaces:**
- Consumes: Task 2 models/errors; `scratch_schema`/`pg_dsn` fixtures.
- Produces:
```python
class PostgresStore:                              # constructed with dsn|pool + schema_name + embedding_dim
    async def migrate(self) -> None               # yoyo programmatic; preflights: pgvector ext,
                                                  # then VECTOR typmod == embedding_dim (EmbeddingDimensionMismatch)
    def transaction(self) -> AsyncContextManager[Tx]
    # all primitives take tx:
    async def lock_decisions(self, tx, ids: Sequence[UUID]) -> dict[UUID, DecisionRow]  # SELECT..FOR UPDATE, ordered by id (deadlock-safe)
    async def insert_decision(self, tx, d: Decision, content_hash: str) -> InsertOutcome  # 'inserted'|'exists_identical'|raises IdempotencyConflict
    async def apply_transition(self, tx, decision_id, action, actor: str, reason, payload, new_status: str | None) -> None
    async def insert_link(self, tx, from_id, to_id, kind: LinkKind) -> None
    async def insert_refs(self, tx, decision_id, refs: Sequence[Ref]) -> None
    async def enqueue(self, tx, kind: QueueKind, decision_id, target_id, proposed_by, rationale, confidence) -> int | None  # None if dedup index blocked (ON CONFLICT DO NOTHING)
    async def resolve_item(self, tx, item_id) -> QueueItem        # guarded UPDATE..AND NOT resolved RETURNING; ItemNotFound/ItemAlreadyResolved
    async def open_items_for(self, tx, decision_id) -> list[QueueItem]
    async def domain_exists(self, conn_or_tx, name) -> bool
    async def upsert_domain(self, tx, name, description, active, actor: str, action: str, reason) -> None  # + domain_transitions row
    async def upsert_embedding(self, tx, decision_id, vector: list[float]) -> None  # validates len == embedding_dim
    async def mark_discovered(self, tx, decision_ids) -> None
```
- Schema is ARCHITECTURE §4 VERBATIM (decisions, links, refs, transitions w/ `new_status`, queue, domains, domain_transitions, embeddings w/ `discovered_at`, all indexes incl. `idx_queue_dedup` and HNSW). All object names schema-qualified via `schema_name`.

- [x] **Step 1: failing migration tests** — migrate on scratch schema creates all tables + `VECTOR(768)` typmod check passes with dim 768 and raises `EmbeddingDimensionMismatch` with dim 384; apply→rollback-last→re-apply cycle; two schemas coexist in one DB.
- [x] **Step 2: failing write tests** — idempotent insert (identical→'exists_identical'; divergent hash→IdempotencyConflict); apply_transition writes transition row WITH new_status and updates decisions.status in the same tx; enqueue dedup (same kind/decision/target twice → second returns None); resolve_item double-tap → ItemAlreadyResolved; lock_decisions ordering by sorted UUID.
- [x] **Step 3–5: implement (yoyo: `read_migrations` from package dir, `get_backend` with search_path/schema handling documented) → GREEN → check.sh+pre-commit → commit** `feat(store): schema migrations and write primitives`

### Task 4: Store reads

**Files:**
- Modify: `application/ports.py`, `adapters/postgres_store.py`
- Test: `tests/db/test_store_reads.py`

**Interfaces (added to PostgresStore; all read-only, plain connection ok):**
```python
async def get_decision(self, decision_id) -> Decision | None
async def get_many(self, ids) -> list[Decision]
async def relevant(self, *, domains, status, tier, subject: tuple[str, str] | None,
                   as_of, text, include_archived, limit, compact_chars) -> list[CompactDecision] | list[Decision]
    # subject-match OR unscoped (FR-6.1); default as_of=now excludes expired valid_until
async def history(self, decision_id) -> HistoryRecord   # decision, refs, transitions, links,
    # predecessor/successor chains (recursive CTE over SUPERSEDES), supplements
async def changes(self, since, actions, actor) -> list[tuple[Transition, CompactDecision]]
async def open_queue(self, kinds, order: Literal["oldest","shakiest","domain"]) -> list[QueueItemView]
    # shakiest: item.confidence, else decision.confidence, else 1.0 last
async def by_source(self, source, **filters) -> list[CompactDecision]
async def knn(self, vector, k, *, exclude_ids=()) -> list[tuple[UUID, float]]   # joined to decisions,
    # archived/discarded excluded via join, over-fetch k*4 internally (04 spec)
async def unembedded(self, limit) -> list[Decision]
async def undiscovered(self, limit) -> list[UUID]        # embeddings WHERE discovered_at IS NULL
async def aging_unrecommended(self, older_than, limit) -> list[CompactDecision]
async def archival_eligible(self, cutoff) -> list[UUID]  # per FR-3.4 incl. open-item block
async def export_rows(self, *, domains, tier, status) -> ExportBundle  # + domains registry rows
```

- [x] **Step 1: failing tests** — seed the §7 narrative fixture (backoff decision, supersession, general-vs-scoped subjects) and assert: relevance scoped/unscoped/status/as_of/expired-default grid; history chains; changes window+actor; shakiest fallback ordering; knn with hand-inserted vectors returns score order and excludes archived; archival_eligible excludes decisions with open items; export bundle includes domains, excludes embeddings.
- [x] **Step 2–4: implement → GREEN → check.sh+pre-commit → commit** `feat(store): reads, projections, candidate enumerations`

### Task 5: Lifecycle Engine

**Files:**
- Create: `src/binnacle/application/lifecycle.py`, `src/binnacle/application/recorder.py`
- Test: `tests/db/test_lifecycle.py` (the transition-matrix + property tests)

**Interfaces:**
- Consumes: store primitives (Task 3/4), domain models.
- Produces (all take `store` + attested `Actor`; each act = ONE store.transaction()):
```python
class LifecycleEngine:
    def __init__(self, store: PostgresStore): ...
    async def record(self, nd: NewDecision, actor: Actor) -> Decision          # tier=short_term; ST-supersedes inline; LT claims → queue(kind='supersede')
    async def record_long_term(self, nd: NewDecision, actor: Actor) -> Decision  # human-only; recorded+promoted transitions
    async def recommend(self, decision_id, actor, reason) -> int | None       # archived → implicit reactivate (both transitions, one tx)
    async def promote(self, item_id, actor: Actor) -> Decision                # human; per 03 acts table incl. pending-claim execution (from = LT copy)
    async def promote_refined(self, source_ids: Sequence[UUID], refined: NewDecision, actor) -> Decision
    async def decline(self, item_id, actor, reason) -> None
    async def discard(self, decision_id, actor, reason) -> None               # FR-3.3 rule; auto-void open items
    async def supersede(self, new_id, old_id, actor) -> None                  # FR-5.2a tier symmetry; acyclicity walk; auto-void
    async def supplement(self, new_id, old_id, actor) -> None
    async def reactivate(self, decision_id, actor) -> None                    # restores prior status via last pre-archive new_status
    async def archive(self, decision_ids, actor: Actor) -> int                # engine actor; used by sweep
    async def apply_item(self, item_id, actor: Actor) -> None                 # human; executes link/supersede items
    async def dismiss_item(self, item_id, actor, reason) -> None
```
- Enforcement per 03: authority pre-check (`AuthorityViolation`), status legality per the FULL exit matrix (03 tables), locks first (`lock_decisions` sorted), fold via `apply_transition(new_status=…)`.

- [x] **Step 1: the exhaustive matrix test** — a table-driven test enumerating (act × actor kind × from-status) with expected outcome (`ok` | `AuthorityViolation` | `InvalidTransition`) exactly mirroring 03's tables; RED first.
- [x] **Step 2: property test** — random legal walk generator (≥200 acts across ≥30 decisions incl. promotions/refinements/supersedes/archivals), then for every decision `status == last non-null new_status in its transitions` and content columns unchanged.
- [x] **Step 3: targeted tests** — refined multi-source consolidation (N links, N promoted, refined:true); pending-LT-claim executes at gate with from=LT copy; cross-tier supersede refused (FR-5.2a); cycle refused; race test (two tasks: promote vs supersede same decision → one `InvalidTransition`, fold holds); recommend-on-archived reactivates.
- [x] **Step 4–5: implement → ALL GREEN → check.sh+pre-commit → commit** `feat(lifecycle): the invariant-bearing engine`

### Task 6: Config + client

**Files:**
- Create: `src/binnacle/application/config.py`, `src/binnacle/application/client.py`, exports in `src/binnacle/__init__.py`
- Test: `tests/unit/test_config.py`, `tests/db/test_client.py`

**Interfaces:** per component spec 01 VERBATIM (`BinnacleConfig` incl. `compact_outcome_chars: int = 200`; `Binnacle` verbs incl. `apply_item`, `dismiss_item`, `update_domain`; typed error surface). `Binnacle.__init__` builds the store lazily (no I/O), `migrate()` delegates; every verb passes the attested Actor through; registry verbs human-only.
- Ports live in `application/ports.py`: `Suggester` / `Embedder` Protocols per ARCHITECTURE §3.1, plus `StubEmbedder` (deterministic hash-based vectors of configured dim) and `ScriptedSuggester` in `tests/` helpers (not shipped).

- [x] **Step 1: failing config tests** — dsn-xor-pool; embedder required; two instances/two schemas coexist; compact_outcome_chars default 200.
- [x] **Step 2: failing client tests** — end-to-end through the public API only (spec 01 acceptance): record (agent) → recommend → promote_refined (human, generalized subjects + amended outcome) → relevant/compact → history shows PROMOTED_FROM + refined payload → export. Plus: promote by non-human raises AuthorityViolation at the client boundary; unknown domain raises UnknownDomain.
- [x] **Step 3–5: implement → GREEN → check.sh+pre-commit → commit** `feat(client): config and public API`

### Task 7: Query service + precedent

**Files:**
- Create: `src/binnacle/application/query.py`
- Modify: `client.py` (wire `relevant/history/precedent/changes/queue/get_many/by_source`)
- Test: `tests/db/test_query.py`

**Interfaces:** thin composition per 04 — `precedent(question, domains, tiers, limit, include_dead)` = `embedder.embed([question])` → `store.knn` → attribute filters → hydrate `PrecedentHit(decision: CompactDecision, similarity: float)` list, superseded/not_promoted included+labeled by status field.

- [x] **Step 1: failing tests** — with StubEmbedder’s deterministic vectors, seed decisions with hand-chosen embedding proximity; assert score order, dead-history inclusion, domain filtering, and that `precedent` never returns archived/discarded.
- [x] **Step 2–4: implement → GREEN → check.sh+pre-commit → commit** `feat(query): relevance, history, precedent`

### Task 8: Sweeps — backfill, discovery, archival

**Files:**
- Create: `src/binnacle/application/discovery.py`, `src/binnacle/application/archival.py`
- Modify: `client.py` (`backfill_embeddings`, `discover`, `archive_stale`)
- Test: `tests/db/test_sweeps.py`

**Interfaces:** per 04 verbatim — backfill validates vector length (EmbeddingDimensionMismatch aborts; batch-atomic otherwise); discover is cursor-driven over `undiscovered()`, structural filters (same domain, subject overlap, temporal order, status compat), taxonomy supersedes/supplements/unrelated, floor+cap, enqueue-dedup tolerated (None returns counted as skipped), `assess_promotion` over `aging_unrecommended`; archive_stale = `archival_eligible(cutoff)` → `lifecycle.archive(ids, Actor("engine","binnacle"))`.

- [x] **Step 1: failing tests** — backfill: drains backlog with StubEmbedder, second run no-ops, wrong-dim embedder aborts with typed error and backlog intact; discover: Suggester call count ≤ N·k asserted (the FR-7.4 mechanical bound), cursor resume after simulated death (kill between classify and mark), dedup on rerun, cap leaves `discovered_at` NULL for overflow, no-suggester no-op; archival: only clock-eligible, open-item block respected, engine actor recorded.
- [x] **Step 2–4: implement → GREEN → check.sh+pre-commit → commit** `feat(sweeps): backfill, discovery, archival`

### Task 9: Export + narrative E2E + perf

**Files:**
- Create: `src/binnacle/application/export.py`; `tests/db/test_narrative_e2e.py`, `tests/db/test_perf.py`
- Modify: `client.py` (export)

**Interfaces:** `export(filter) -> dict` JSON-safe bundle: decisions (+refs, +links, +transitions), domains registry, `schema_version`, no embeddings (FR-6.6).

- [x] **Step 1: export tests** — content per FR-6.6; JSON-serializable; spot re-hydration equality.
- [x] **Step 2: narrative E2E** — encode REQUIREMENTS §7 as one test: agent records backoff decision → same-session supersede → recommendation → human refined-promote (jitter + all-remote-calls) → supplement later → supersede by human → archival of stale ST noise → every §7 claim asserted (statuses, links, transitions, queue states).
- [x] **Step 3: perf seed test** (`@pytest.mark.perf`) — seed 10k decisions/100k transitions via bulk COPY/executemany; assert NFR-7 targets with a 4× CI multiplier; record measured numbers in the report.
- [x] **Step 4–5: GREEN → check.sh+pre-commit → commit** `feat(export): JSON export; narrative and perf suites`

### Task 10: Close-out — docs, README, CI green, push

**Files:**
- Create: `README.md`
- Modify: `docs/ARCHITECTURE.md` (P-1 driver confirmed), plan checkboxes

- [x] **Step 1:** Full `scripts/check.sh` + `pre-commit run --all-files` + perf marker run locally; fix nothing silently — report.
- [x] **Step 2:** README: install, provisioning preconditions (createdb + CREATE EXTENSION vector), 25-line embedding example (config → migrate → record → recommend → promote → precedent), the guardrail stack (pre-commit/CI), test env var, actor attestation note, limitations (v2 list pointer).
- [x] **Step 3:** Update ARCHITECTURE P-1 (driver: psycopg3 confirmed + chosen pins); tick plan checkboxes.
- [ ] **Step 4:** Commit `docs: close out phase 1`; **push `origin main`** (owner-authorized); verify CI runs green on GitHub (watch the run via `gh run watch` or report the URL + first status).

## Self-Review (performed)

- **Spec coverage:** FR-1→T2/T3/T5, FR-2→T3/T6, FR-3→T3/T5/T8, FR-4→T5/T6, FR-5→T5, FR-6→T4/T7/T9, FR-7→T6(ports)/T8, FR-8→T6; NFR-1→T5 property test, NFR-4→T3, NFR-7→T9 perf; guardrails (owner reqs)→T1; §7 narrative→T9; every reviewer-fix (fold/new_status, locks, queue hygiene, dedup index, cursor, tier symmetry, IdempotencyConflict, dimension preflight) has a named test in T3/T5/T8.
- **Placeholder scan:** clean; pins say `<current exact>` deliberately — chosen at task time and recorded (house pattern).
- **Type consistency:** interface blocks in T2/T3/T4/T5 are the single sources; later tasks reference those names verbatim.
