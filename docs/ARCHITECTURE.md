# ARCHITECTURE

Architecture for **Binnacle** (contract: `docs/REQUIREMENTS.md`). Binnacle is a
Python library — the fleet's decision record and precedent engine — storing
decisions in one PostgreSQL database (relational tables + pgvector), embedded by
the meridian service. Semantica's decision subsystem informed the design (NFR-3)
but is not a dependency in v1.

## 1. Architectural Position

```
meridian (UI, authn/authz, jobs)          agents (via meridian tools)
        │ record / recommend / promote / query        │ record / precedent
        ▼                                             ▼
   ┌────────────────── binnacle (library) ──────────────────┐
   │ application: recorder, lifecycle, queue, query,        │
   │              discovery, archival, export               │
   │ ports: Suggester, Embedder                             │
   │ domain: Decision, Ref, Link, Transition, Actor, enums  │
   │ adapters: postgres store (tables + pgvector)           │
   └────────────────────────────────────────────────────────┘
        │ SQL + pgvector (one database, one store)
        ▼
   PostgreSQL 18 + pgvector
```

### 1.1 Principles

1. **The record is the product.** Content immutable, transitions append-only,
   every change attributed. Anything that would make the record unable to prove
   "what was believed, by whom, when" is wrong by definition (NFR-1).
2. **Suggest / mechanize / gate.** LLMs only suggest (never commit), clocks and
   indexes mechanize what needs no judgment, humans gate every long-term mutation
   (FR-4, FR-5.2, FR-7). The gate is enforced in the write path, not by convention.
3. **One store, one transaction.** Every lifecycle act commits atomically in the
   relational store (NFR-4). Nothing is stored in two places; there is no derived
   store to diverge. pgvector embeddings are the single exception — derived,
   asynchronously backfilled, and rebuildable (I-5).
4. **Own the small core; borrow only what pays** (NFR-3). The decision store is
   small enough to own outright; semantica remains reference material with named
   v2 adoption triggers.
5. **Library, not authority** (FR-8). No daemon, no env/file reads, config-object
   initialization, multiple instances allowed; meridian authorizes callers,
   binnacle records attested actors.

## 2. Context (C4 L1)

- **meridian** — the embedder: exposes recording/queue/query over its UI, API, and
  MCP surface; runs the sweep jobs; fulfills the `Suggester` port via tradewind
  (light tier) and the `Embedder` port; authenticates callers and attests actor
  kinds.
- **Agents** (tradewind sessions) — record decisions and consult precedent through
  meridian-supplied tools.
- **Source systems** (portolan, waypoint, sextant tooling) — record their
  *policy-level* decisions; operational provenance stays home (REQUIREMENTS §5).
- **PostgreSQL 18 + pgvector** — the one database (verified on the target host).

## 3. Components (C4 L2)

| Component | Responsibility |
|---|---|
| **Recorder** | FR-1 write path: registry/ref validation, idempotent insert (caller ids), `decided_at`/`recorded_at`, declared relationships, human-only direct-to-long-term (FR-4.4) and refined/consolidating promotion content (FR-4.6). Enqueues embedding backfill. |
| **Domain Registry** | FR-2: governed domain list; human-only changes, transition-logged. |
| **Lifecycle Engine** | FR-3/4/5: the ONLY writer of statuses, links, and transitions; enforces both state machines and the authority rule (I-2); executes promotion, supersession, supplements, archival, re-activation — each as one transaction (I-1). |
| **Review Queue** | FR-4.3: pending-promote / pending-link / pending-supersede; ordering; resolutions delegate to the Lifecycle Engine. |
| **Query Service** | FR-6: relevance (subject-or-unscoped, lexical filter), history (CTE over links + transitions), changes feed/audit, projections (full/compact), direct access, candidate enumeration, **precedent search** (pgvector k-NN + attribute filters, both tiers). |
| **Discovery Pipeline** | FR-7.2/7.4: at backfill, k-NN candidates → structural filters → `Suggester` → capped, confidence-floored queue items. |
| **Archival Sweep** | FR-3.4: clock-driven `archived` transitions via the Lifecycle Engine. |
| **Exporter** | FR-6.6: filtered JSON export (decisions + links + transitions). |
| **Postgres Store** | Schema (§4), migrations, partial indexes, transactions, pgvector operations. The only adapter. |

### 3.1 Ports

```python
class Suggester(Protocol):
    async def classify_pairs(self, pairs: list[CandidatePair]) -> list[Suggestion]: ...
    async def assess_promotion(
        self, decisions: list[CompactDecision]
    ) -> list[PromotionAssessment]: ...


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
```

Core never constructs an LLM or embedding client (FR-7.1). Meridian fulfills both
(tradewind light tier; embeddings via nomic-embed-text-v1.5 per OQ-3). Development/test fulfillment: a
deterministic stub embedder and a scripted suggester.

## 4. Domain Model and Schema

One store, both tiers. Content columns are never UPDATEd (I-3); `status` and
`links` change only through the Lifecycle Engine.

```sql
CREATE TABLE decisions (
  decision_id     UUID PRIMARY KEY,            -- caller-supplied or minted (FR-1.6)
  tier            TEXT NOT NULL,               -- 'short_term' | 'long_term'
  domain          TEXT NOT NULL REFERENCES domains(name),
  status          TEXT NOT NULL,               -- denormalized fold of transitions (I-1)
  scenario        TEXT NOT NULL,
  outcome         TEXT NOT NULL,
  reasoning       TEXT NOT NULL,
  options_considered JSONB NOT NULL DEFAULT '[]',   -- [{option, why_rejected}]
  consequences    TEXT,
  confidence      REAL,                        -- optional triage signal (FR-1.1)
  source          TEXT NOT NULL,
  content_hash    TEXT NOT NULL,               -- FR-1.6 idempotency key: sha256 of canonical
                                               -- content JSON (NewDecision.content_hash()),
                                               -- persisted at insert time so retries compare
                                               -- against the hash algorithm in effect when the
                                               -- row was written, never a recomputation
  recorded_by     TEXT NOT NULL,               -- attested actor "kind:id" (I-2)
  decided_at      TIMESTAMPTZ NOT NULL,        -- FR-1.7 (defaults to recorded_at)
  recorded_at     TIMESTAMPTZ NOT NULL,
  valid_from      TIMESTAMPTZ,
  valid_until     TIMESTAMPTZ,
  metadata        JSONB NOT NULL DEFAULT '{}',
  schema_version  INT NOT NULL DEFAULT 1
);

CREATE TABLE links (                           -- all inter-decision relationships
  from_id UUID NOT NULL REFERENCES decisions(decision_id),
  to_id   UUID NOT NULL REFERENCES decisions(decision_id),
  kind    TEXT NOT NULL,                       -- 'SUPERSEDES' | 'SUPPLEMENTS' | 'PROMOTED_FROM'
  PRIMARY KEY (from_id, kind, to_id)
);

CREATE TABLE refs (
  decision_id UUID NOT NULL REFERENCES decisions(decision_id),
  role        TEXT NOT NULL,                   -- 'subject' | 'evidence'
  kind        TEXT NOT NULL,                   -- open: component, product, market, session, url, ...
  identifier  TEXT NOT NULL,
  note        TEXT,
  PRIMARY KEY (decision_id, role, kind, identifier)
);

CREATE TABLE transitions (
  transition_id BIGSERIAL PRIMARY KEY,
  decision_id   UUID NOT NULL REFERENCES decisions(decision_id),
  action        TEXT NOT NULL,                 -- recorded|recommended|promoted|declined|discarded|
                                               -- superseded|supplement_linked|archived|reactivated|...
  actor         TEXT NOT NULL,                 -- "kind:id" (kind ∈ human|agent|engine)
  at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  reason        TEXT,
  new_status    TEXT,                          -- resulting status when the action changes one;
                                               -- fold(transitions) = last non-null new_status (I-1)
  payload       JSONB                          -- {"target": ...}; {"item_id": ...} on resolutions
);

CREATE TABLE queue (
  item_id     BIGSERIAL PRIMARY KEY,
  kind        TEXT NOT NULL,                   -- 'promote' | 'link' | 'supersede'
  decision_id UUID NOT NULL REFERENCES decisions(decision_id),
  target_id   UUID REFERENCES decisions(decision_id),
  proposed_by TEXT NOT NULL, proposed_at TIMESTAMPTZ NOT NULL,
  rationale   TEXT, confidence REAL,
  resolved    BOOLEAN NOT NULL DEFAULT FALSE   -- resolution detail lives in transitions
);

CREATE TABLE domains (
  name TEXT PRIMARY KEY, description TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE embeddings (
  decision_id   UUID PRIMARY KEY REFERENCES decisions(decision_id),
  embedding     VECTOR NOT NULL,               -- dimension fixed by config at migration time
  embedded_at   TIMESTAMPTZ NOT NULL,
  discovered_at TIMESTAMPTZ                    -- discovery cursor: NULL = not yet swept (FR-7.4);
                                               -- over-cap rows stay NULL, picked up next sweep
);

CREATE TABLE domain_transitions (              -- FR-2.2 registry audit (no decision row to attach to)
  id BIGSERIAL PRIMARY KEY, domain TEXT NOT NULL, action TEXT NOT NULL,
  actor TEXT NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now(), reason TEXT
);

-- Hot-path partial indexes (NFR-5/NFR-7): active working set only.
CREATE INDEX idx_dec_active   ON decisions(tier, domain, status) WHERE status NOT IN ('archived','discarded');
CREATE INDEX idx_refs_subject ON refs(kind, identifier) WHERE role = 'subject';
CREATE INDEX idx_links_to     ON links(to_id, kind);
CREATE INDEX idx_trans_time   ON transitions(at DESC);
CREATE INDEX idx_trans_actor  ON transitions(actor, at DESC);
CREATE INDEX idx_queue_open   ON queue(kind, proposed_at) WHERE NOT resolved;
CREATE UNIQUE INDEX idx_queue_dedup ON queue(kind, decision_id,
  COALESCE(target_id, '00000000-0000-0000-0000-000000000000'::uuid))
  WHERE NOT resolved;                          -- discovery re-runs cannot duplicate open items
-- pgvector index (HNSW) on embeddings.embedding, filtered joins exclude archived.
```

`decisions.content_hash` (added during Task 3 implementation, not in the original
schema draft): idempotent insert (FR-1.6) needs a persisted comparison key —
recomputing the hash from stored columns on every conflict would duplicate
`NewDecision.content_hash()`'s canonical-JSON logic inside the store and silently
drift if that formula ever changes. Storing the hash alongside the other
immutable content columns keeps it a stable fact-at-write-time, consistent with
I-3.

Structural queries are recursive CTEs over `links` (supersession chains,
supplement networks) — the same pattern as tradewind's session tree. When a
decision is superseded or discarded, its open queue items auto-resolve as
`voided` in the same transaction (FR-4.3); open items block archival (FR-3.4).

### 4.1 Schema ownership and migrations

All tables live in binnacle's **own PostgreSQL schema** (default `binnacle`,
configurable) — the ownership boundary inside the shared database: no collisions
with the embedder's or siblings' tables, per-schema permissions, contained blast
radius. The division of responsibility:

- **Host (embedder/operator)**: the database, the role, and extensions
  (`CREATE EXTENSION vector` requires elevated rights — a provisioning
  precondition binnacle checks and reports, never performs).
- **Binnacle**: ships ordered SQL migrations via **yoyo-migrations** (chosen over
  a hand-rolled runner: rollback steps come for free — incident-driven schema
  rollback cannot be ruled out, and an untested improvised down-path is worse
  than a shipped one; yoyo's own version/lock bookkeeping tables are acceptable
  wherever they land). Every migration ships an apply step and, where physically
  possible, a rollback step. Exposed as a single `migrate(conn_or_dsn)` library
  function (yoyo's programmatic API); binnacle never migrates implicitly —
  **the host decides when to call it** (startup or deploy step). Note: schema
  rollback ≠ record mutation — decisions/transitions stay append-only (NFR-1);
  rollback exists for structural incidents only.

`BinnacleConfig` accordingly carries `schema` (name) alongside the DSN/pool.

### 4.2 Invariants

- **I-1** Every `decisions.status` equals the fold of that decision's transitions,
  where the fold is COMPUTABLE by construction: status-changing transitions
  record their `new_status`, and the fold is the last non-null one (reactivation
  writes the restored status explicitly). The Lifecycle Engine is the only writer
  of `status`, `links`, and `transitions`; each act commits in one transaction,
  opening with `SELECT … FOR UPDATE` on every touched decision row — concurrent
  acts serialize, so validate-then-write races cannot occur. Queue resolution is
  guarded the same way (`UPDATE … SET resolved = TRUE WHERE item_id = $1 AND NOT
  resolved` must return a row — a double-tap resolves once).
- **I-2** Long-term mutations (promotion, superseding or linking a long-term
  decision) require a **human** actor. Actors are typed —
  `Actor(kind: human|agent|engine, id)`, stored `"kind:id"` — and meridian attests
  the kind (FR-8.2); the Lifecycle Engine enforces the rule against the attested
  kind. Id honesty WITHIN a kind (e.g. FR-3.3's discard-own rule comparing agent
  ids) is the embedder's enforcement duty — binnacle trusts the attested id; that
  is the documented boundary of "attribution, not authorization".
- **I-3** Decision content columns are never UPDATEd after insert.
- **I-4** Suggestions (queue rows, `Suggester` output) touch nothing outside
  `queue` until a human resolution passes through the Lifecycle Engine.
- **I-5** Recording never awaits the `Embedder` (NFR-7): `embeddings` is a derived,
  asynchronously backfilled table; a missing embedding degrades precedent recall,
  never correctness.

## 5. Key Interactions

- **Record (agent)**: validate domain/refs → idempotent insert (`tier='short_term'`,
  `status='current'`) + `recorded` transition — one transaction. Declared
  `supersedes` of a short-term target executes in the same transaction (link +
  `superseded` transition on the target); of a long-term target, files a
  pending-supersede queue item instead (I-2).
- **Recommend → promote**: recommendation (any actor) = queue item + transition.
  Human resolution of a promote item — all in one transaction: insert the
  long-term row (verbatim copy, or the human's **refined** decision per FR-4.6 —
  possibly consolidating several sources), `PROMOTED_FROM` link(s) to every
  source, execute any pending supersede claims (SUPERSEDES links whose `from` is
  the NEW long-term copy — FR-5.2a — plus `superseded` transitions on the
  long-term targets), mark each source `promoted`, resolve the queue item(s),
  write all transitions (refined promotions carry `refined: true`; every
  resolution carries `item_id` in its payload).
- **Direct long-term record (human, FR-4.4)**: one transaction inserting the
  long-term row with `recorded` + `promoted` transitions.
- **Discovery sweep** (meridian job): drain the backfill (batch `Embedder.embed`,
  insert `embeddings`) → per newly embedded decision, k-NN + structural filters
  (FR-7.4) → `Suggester.classify_pairs` → queue items above the confidence floor,
  capped per sweep.
- **Archival sweep**: one bulk transaction of clock-eligible `archived` transitions
  (FR-3.4).
- **Precedent query**: embed the question (`Embedder`) → pgvector k-NN over
  non-archived embeddings joined to `decisions` with tier/domain/status filters →
  hydrate compact or full projections (FR-6.7).
- **Changes feed / audit**: indexed scans of `transitions` by window/actor/action.
- **Export**: filtered `decisions` + their `links`, `refs`, `transitions` as JSON.

## 6. Package Layout and Technology

```
src/binnacle/
  domain/        models.py (Decision, Ref, Link, Transition, Actor, enums,
                 CandidatePair, Suggestion, projections)  errors.py
  application/   client.py recorder.py lifecycle.py queue.py query.py
                 discovery.py archival.py export.py ports.py config.py
  adapters/      postgres_store.py
```

- Python ≥3.13, async-first; pydantic v2 config object (`BinnacleConfig`: DSN or
  caller-supplied pool, schema name (§4.1), embedding dimension, archival policy,
  discovery caps/floor; `Suggester`/`Embedder` supplied live,
  tradewind-broker-style).
- Postgres driver: **psycopg3 (async)** — first choice for one-dependency
  parameterized SQL + pgvector adaptation; confirm at plan time (P-1).
- Layering by import-linter: `adapters → application → domain`; `domain` imports no
  DB driver. mypy strict; house gates (`scripts/check.sh`) per GUIDELINES.
- Dependencies: driver + pydantic (+ pgvector adapter helper if needed). **No
  semantica** (NFR-3).

### 6.1 Decision records

- **DR-1 Single relational store; no graph layer in v1.** Rejected: AGE-as-index
  (dual writes, divergence class, rebuild tooling for a marginal ranking boost and
  CTE-answerable traversals). The `links` table + recursive CTEs carry v1
  structure; a graph can be populated from `decisions + links` at any time (v2
  trigger in REQUIREMENTS §5).
- **DR-2 Core is LLM-free and embedder-free** (sextant DR-2 lineage): both arrive
  as ports; recording never blocks on either (I-5).
- **DR-3 Semantica-informed, zero-dependency v1** (NFR-3): the store is small
  enough to own; borrowing would have cost a facade, a pin, and a pre-1.0 churn
  surface with no offsetting effort saved in the single-store design.
- **DR-4 One transaction per lifecycle act** (I-1) — the design's integrity
  primitive; no cross-store ordering rules exist because there is no second store.
- **DR-5 Attribution, not authorization** — meridian gates callers and attests
  actor kinds; binnacle refuses only state-machine and authority-rule violations.

## 7. Pending Decisions

- **P-1** Driver confirmation (psycopg3-async vs asyncpg) — settle at plan time
  with a spike if pgvector adaptation proves awkward.
- **P-2** ~~Embedding model~~ **Resolved:** `nomic-embed-text-v1.5`, 768-dim,
  8192-token context (OQ-3) — fulfilled by the embedder-side `Embedder` adapter;
  binnacle's schema fixes `VECTOR(768)` at migration; the embedded text is
  `scenario + outcome + reasoning` concatenated; tests use the deterministic
  stub.
- **P-3** v2 items per REQUIREMENTS §5 — graph layer (AGE + semantica machinery)
  with its named triggers; conflict detection; scope hierarchy; Parquet offload;
  Markdown export; transcript extraction.
