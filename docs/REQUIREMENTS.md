# REQUIREMENTS

Requirements for **Binnacle**, a general-purpose decision record and precedent engine
for the sibling projects (meridian, portolan, tradewind, waypoint) and their human
operator. Decisions from any domain — product, architecture, UX, testing, document
modeling, corporate policy — are recorded with rationale and evidence, curated through
a human gate into a durable record, and drawn on as precedent when deciding again.

Grounding: semantica's decision subsystem (`semantica.context`) verified against
source v0.6.7 in the 2026-09-01 session; Apache AGE 1.8.0 + pgvector installed and
smoke-tested on the target host.

## 1. Problem Statement

Decisions are made constantly — by humans in design discussions and by agents
mid-workflow — and then lost: buried in transcripts, chat logs, and memory. When the
same question recurs, the reasoning is reconstructed from scratch, prior rejections
are re-litigated, and contradictory choices accumulate unnoticed. ADR files capture a
sliver (architecture only, humans only, one repo at a time) and offer no querying, no
lifecycle, and no precedent search. Binnacle makes the decision itself a first-class,
queryable, auditable record with a curation gate between working noise and durable
policy.

## 2. Glossary

- **Decision** — one recorded choice: the question faced, the outcome, the reasoning,
  and its evidence. Content is immutable once recorded.
- **Tier** — where a decision lives: **short-term** (the working record; anything may
  land here) or **long-term** (the durable record; entered only through the human gate).
- **Domain** — the subject lane a decision is *about* (architecture, product, ux,
  testing, documents, hiring, …). Drawn from a governed registry.
- **Subject ref** — what a decision *applies to* (a component, product, market,
  document node). A decision with no subject refs applies generally within its domain.
- **Evidence ref** — what supports a decision (a tradewind session, spike doc,
  benchmark, URL).
- **Source** — the system or person that recorded a decision (meridian, portolan, a
  named human, `binnacle-engine`).
- **Actor** — the identity performing a lifecycle transition (human id, agent id,
  `binnacle-engine`).
- **Transition** — one append-only lifecycle event on a decision: (action, actor,
  timestamp, reason). The audit trail is the transition log.
- **Recommendation** — a *pending* transition awaiting a human: pending-promote,
  pending-link, pending-supersede. Recommendations never change the record by
  themselves.
- **Promotion** — the human-gated copy of a short-term decision into the long-term
  tier. Copy-only: the source row is never moved or deleted.
- **Supersede** — a decision replaces another; the old one remains readable, marked
  superseded, linked to its successor.
- **Supplement** — a decision qualifies or extends another that remains current;
  expressed as a relationship, never a status.
- **Precedent search** — hybrid (embedding + graph) retrieval of prior decisions
  similar to a question at hand, deliberately including superseded and declined
  history.

## 3. Functional Requirements

### FR-1 Recording decisions
- **FR-1.1** Any authorized caller (human via the embedding service, or an agent)
  records a decision into the **short-term tier** with: `domain` (registry-validated),
  `scenario`, `outcome`, `reasoning`, `source`, recording actor, optional
  `options_considered` (rejected alternatives, each with a one-line why), optional
  `consequences`, optional `confidence` (0–1; a triage signal, primarily meaningful
  for agent sources), optional `valid_from`/`valid_until`, subject refs, evidence
  refs, and a free-form metadata escape hatch.
- **FR-1.2** Refs are typed: `Ref(role: subject|evidence, kind, identifier, note)`.
  Kinds are open strings (component, product, market, document, session, url, file, …).
- **FR-1.3 Decision content is immutable.** There is no edit and no delete. A
  correction is a new decision that supersedes the old; junk is `discarded` (hidden
  from default reads, never removed). Physical deletion is out of scope for v1.
- **FR-1.4** Recording MAY declare relationships at write time (`supersedes=`,
  `supplements=` targeting existing decisions), subject to FR-5's authority rule.
- **FR-1.5** The two identity axes are distinct and both required conceptually:
  `domain` (what it is about) and subject refs (what it applies to; absence = general).
  No additional `type`/`category` taxonomy fields exist.
- **FR-1.6 Idempotent recording.** The caller MAY supply the decision id (UUID);
  recording the same id twice is a no-op returning the existing decision, so agent
  retries never duplicate. Absent an id, Binnacle mints one.
- **FR-1.7 Backfill.** Recording accepts an optional `decided_at` (when the decision
  was actually made — for importing pre-existing decisions such as historical ADRs)
  distinct from `recorded_at`, which is always system-set. Absent `decided_at`,
  they coincide.

### FR-2 Domain registry
- **FR-2.1** Domains live in a governed registry (name + description). Writers MUST
  use a registered domain; an unregistered domain is an error, never an implicit
  creation.
- **FR-2.2** Registry changes (add, rename-description, deactivate) are human-only
  actions and are themselves transition-logged.

### FR-3 Lifecycle, transitions, audit
- **FR-3.1** Every lifecycle event is an append-only transition:
  `(decision_id, action, actor, timestamp, reason?, payload?)`. The transition log is
  the audit trail; a decision's current `status` is a denormalized view of it. "Who
  recorded / recommended / promoted / declined / discarded / superseded, and when" are
  all answered from transitions — no per-action audit columns.
- **FR-3.2** Short-term statuses: `current | promoted | not_promoted | superseded |
  discarded | archived`. Long-term statuses: `current | superseded`. `supplemented`
  is not a status (FR-5.3). Temporal expiry is `valid_until`, orthogonal to status.
- **FR-3.3** `not_promoted` means *considered at the gate and declined* (kept as
  signal); `discarded` means *not a real decision* (noise, malformed, duplicate).
  Discard is permitted to the recording actor for its own short-term `current`
  decisions, and to humans for any short-term decision.
- **FR-3.4 Auto-archival (the working-set bound).** A short-term decision untouched
  for a configurable age (default 90 days: `current` with no transitions since, or
  `not_promoted` never re-recommended) receives an automatic `archived` transition
  (actor `binnacle-engine`, clock-driven — mechanism, not judgment, per FR-7.3).
  Archived decisions are excluded from default relevance, precedent, and queue reads
  and from the hot embedding index, but remain fully retrievable
  (`include_archived`) and re-activatable by a transition (re-recommendation
  un-archives). Long-term decisions are never auto-archived. This keeps the active
  working set bounded regardless of total record growth; nothing violates
  append-only.

### FR-4 Promotion and the review queue
- **FR-4.1** Promotion copies a short-term decision into the long-term tier (new id,
  provenance link to the source; source status → `promoted`). **Only a human may
  execute promotion.**
- **FR-4.2** Anyone — an agent, the engine sweep, or a human — may file a
  **promotion recommendation** on a short-term `current` decision (actor + reason
  recorded). Recommendations from all recommenders land in one review queue.
- **FR-4.3** The review queue lists pending items (pending-promote, pending-link,
  pending-supersede) with ordering support (age, confidence, domain). A human
  resolves each item: execute, decline (`not_promoted` for promotions; dismiss for
  links), or defer. Every resolution is a transition with actor and reason.
- **FR-4.4 Direct long-term recording.** A human MAY record a decision directly into
  the long-term tier as one atomic act (semantically: record + promote, both
  transitions logged). Human-only — the gate is preserved; agents always land in
  short-term.
- **FR-4.5 Decline is not terminal.** A `not_promoted` decision may later be
  re-recommended and promoted; the earlier decline remains visible in its transition
  log. Only `superseded` and `discarded` are terminal.

### FR-5 Relationships between decisions
- **FR-5.1** Relationship kinds: `SUPERSEDES`, `SUPPLEMENTS`, plus the internal
  `PROMOTED_FROM` provenance link. Relationships are established by: (a) declaration
  at recording time, (b) confirmation of an engine suggestion, (c) post-hoc human
  curation. Adding a relationship is an append.
- **FR-5.2 Authority rule:** any relationship or status change that mutates a
  **long-term** decision (superseding it, linking it) executes only through the human
  gate. An agent's recorded claim that its short-term decision supersedes long-term
  D executes at promotion time, by the promoting human, in one act. Short-term ↔
  short-term supersession is ungated (it is the working record).
- **FR-5.3** A supplemented decision remains `current`; reads surface its supplements
  alongside it. Binnacle records relationships between decisions; it does NOT
  adjudicate precedence between them (no rules engine) — meaning is resolved by the
  reader and expressed through curated relationships.

### FR-6 Queries

Consumer → capability map (each row traceable to the FRs below):

| Consumer | Needs | FRs |
|---|---|---|
| Human operator (curation) | review queue, changes-since, domain dossier, full history, lexical + precedent search | 6.4, 6.5, 6.1, 6.2, 6.3 |
| Agent mid-work (via meridian tools) | compact top-N relevance for context injection, precedent check, follow refs by id | 6.7, 6.3, 6.8 |
| Software-factory tasks (TDD generation) | cross-domain current set for a subject + general, as-of filtering | 6.1 |
| Source systems (portolan, …) | list own decisions by source, idempotent existence/batch get | 6.8, 1.6 |
| Auditor | transitions by actor/time, export | 6.5, 6.6 |
| Engine sweep (via ports) | candidate enumeration (unrecommended aging short-term; unembedded backlog; lookalike pairs) | 6.9 |

- **FR-6.1 Relevance:** decisions by domain list (default: all domains — cross-domain
  reads are the norm), status filter (default: current), tier, and subject: the
  relevance query for subject X returns decisions whose subject refs include X **or**
  that are unscoped, within the chosen domains. As-of temporal filtering honors
  `valid_from/until`. An optional lexical text filter (substring/keyword over
  scenario/outcome/reasoning) narrows results without invoking semantic search.
- **FR-6.2 History:** a decision's full record — content, transitions, relationships,
  predecessor/successor chains — including superseded, declined, and discarded
  entries when explicitly requested.
- **FR-6.3 Precedent:** hybrid search (embedding similarity + graph context) for
  decisions similar to a stated question, across both tiers and all domains by
  default, including superseded/`not_promoted` history — how thinking evolved is part
  of the answer.
- **FR-6.4** Queue reads per FR-4.3.
- **FR-6.5 Changes feed / audit view:** transitions queryable by time window, action
  kind, and actor — serving both "what was decided or promoted since T?" (a human
  catching up, an agent rejoining work) and "everything actor A did" (audit).
- **FR-6.6 Export:** any filtered decision set (with transitions and relationships)
  exportable as JSON for backup and portability. Markdown/ADR-file rendering is a v2
  candidate (§5).
- **FR-6.7 Projections.** Every read supports a compact projection (id, domain,
  outcome, status, one-line summary, subject refs) alongside the full record, with
  top-N limits — sized for agent context injection, where full reasoning blobs are
  a token budget hazard. Field selection is explicit, never inferred.
- **FR-6.8 Direct access.** Batch get-by-id (following refs from other systems) and
  list-by-source (a source system enumerating its own decisions), with the standard
  status/tier filters.
- **FR-6.9 Candidate enumeration (for the assist sweeps).** Efficient queries for:
  short-term `current` decisions older than T with no promotion recommendation;
  decisions lacking embeddings (the backfill backlog); hybrid-shortlist pairs for
  relationship suggestion. These serve FR-7.2 and run within NFR-7 targets.

### FR-7 Assist layer — LLM suggests, mechanism decides, humans gate
- **FR-7.1** Binnacle core NEVER calls an LLM (sextant DR-2 inherited). It defines
  two ports: `Suggester` (candidate bundles in → classified suggestions out) and
  `Embedder` (text → vector), fulfilled by the embedding service (meridian via
  tradewind's light tier).
- **FR-7.2** Engine assistance produces only pending queue items: relationship
  suggestions (supersedes/supplements/conflicts/related, with rationale) over
  hybrid-search shortlists; promotion-candidate sweeps over short-term `current`
  decisions; supersession/contradiction flags. `source=binnacle-engine`, never
  auto-committed.
- **FR-7.3** Deterministic mechanisms need neither LLM nor human: expiry by clock,
  registry validation, hybrid shortlisting, queue ordering, auto-archival.
- **FR-7.4 Discovery scales linearly, never quadratically.** Relationship discovery
  MUST be incremental: when a decision is embedded, candidates are its top-k vector
  neighbors (k configurable, default ≤10) surviving structural filters (same domain,
  subject overlap, temporal order, status compatibility); only survivors reach the
  `Suggester`. Work per new decision is O(k); a full-record pairwise sweep is never
  performed. Suggestions below a confidence floor are dropped and each sweep's queue
  contribution is capped, so the review queue stays bounded regardless of engine
  enthusiasm.

### FR-8 Packaging and embedding
- **FR-8.1** Binnacle is a **library** (like tradewind): no daemon, no file/env/global
  reads; initialized with a single caller-constructed config object (database
  connection/DSN, embedder port, options). Multiple independently configured
  instances may coexist.
- **FR-8.2** The embedding service owns authentication and decides which callers may
  invoke which operations; Binnacle enforces *attribution* (actors on every
  transition), not authorization.
- **FR-8.3** Async-first API, mypy-strict, house layering (GUIDELINES.md).

## 4. Non-Functional Requirements

- **NFR-1 Integrity.** Content immutable; transitions append-only; nothing physically
  deleted; every state change attributable to an actor with a timestamp. The record
  must be able to prove what was believed, by whom, when.
- **NFR-2 Honest assistance.** No suggestion, however confident, mutates the record;
  the gate is structural (enforced in the write path), not conventional.
- **NFR-3 Semantica reuse with a typed boundary.** Reuse `semantica.context`'s
  `Decision` model, hybrid `DecisionQuery`, embedding pipeline, and `ApacheAgeStore`
  directly, pinned exact. Binnacle-specific fields (domain, source, status, refs,
  recommendations) ride in `Decision.metadata`, written and read ONLY through a typed
  facade — application code never touches raw metadata keys. Semantica's
  `DecisionRecorder` is wrapped (its Cypher `CREATE` is non-idempotent).
- **NFR-4 One database.** PostgreSQL with Apache AGE and pgvector — no additional
  infrastructure (Postgres 18 + AGE 1.8.0 + pgvector verified on the target host).
  Per-tier storage layout is an ARCHITECTURE decision; the working direction matches
  each tier's access pattern: **short-term as relational tables** (attribute-filtered
  queries, trivial archival/export, at most a supersession-chain column) and
  **long-term as the AGE graph** (supersession/supplement structure queried
  structurally). Embeddings in pgvector span both tiers. The active working set is
  kept fast by partial indexes over non-archived rows — never by moving bytes out of
  the database.
- **NFR-5 Scale honesty.** Designed for thousands of *active* decisions, not
  millions: correctness and auditability over throughput. Two mechanisms bound the
  working set as the total record grows without limit: the §5 scope rule (no
  operational event logging keeps junk out) and FR-3.4 auto-archival (age moves the
  stale out). NFR-7's targets bind to the active working set (archived excluded).
- **NFR-6 House standards.** mypy strict, ruff, import-linter layering, TDD, exact
  pins for semantica (pre-1.0 — expect churn behind the facade).
- **NFR-7 Performance.** Targets at the design scale of 10,000 decisions /
  100,000 transitions, measured store-side on the target host (excluding any
  `Embedder`/`Suggester` port latency, which belongs to the fulfilling service):

  | Operation | Target (p95) |
  |---|---|
  | Record a decision | < 250 ms (never blocks on embedding — see below) |
  | Relevance query (FR-6.1) | < 200 ms |
  | Single-decision history (FR-6.2) | < 100 ms |
  | Precedent search, store-side (FR-6.3) | < 500 ms |
  | Queue read (FR-6.4) | < 200 ms |
  | Changes feed (FR-6.5) | < 200 ms |
  | Promotion (copy + edges + transitions) | < 500 ms |

  **Write/embed decoupling:** recording completes without calling the `Embedder`;
  embeddings are backfilled asynchronously (a decision is precedent-searchable
  once backfilled, relevance-queryable immediately). A slow or unavailable
  embedding service must never stall decision capture. Targets are verified by a
  seeded perf test in the suite (house pattern: generous CI bound over the
  measured local number).

## 5. Out of Scope

- **An operational audit log for source systems.** Portolan's per-item curation
  dispositions, waypoint's visit records, meridian's job events stay in their own
  systems' provenance. Binnacle stores decisions worth *consulting later* — rationale
  with reuse value — not high-volume operational events.
- **Authorization** (roles, per-domain ACLs) — the embedding service's concern.
- **Precedence adjudication** between overlapping decisions (FR-5.3).
- **Physical deletion / retention purges** (revisit if compliance ever requires).
- **Demotion.** No un-promote: a mistaken promotion is corrected by superseding the
  long-term decision, keeping the mistake visible.
- **Notifications.** Alerting on queue items is the embedding service's concern
  (jobs/UI), not the library's.
- **v2 candidates, named for later:** automated conflict detection across current
  decisions (semantica's `conflicts` module); hierarchical scope registry
  (company → department → team inheritance); `DecisionContext` snapshots
  (entity_snapshots / risk_factors) as structured attachments; decision extraction
  from transcripts; Markdown/ADR-file export rendering (JSON export is v1, FR-6.6);
  cold offload of archived short-term decisions to columnar files (e.g. Parquet) —
  unnecessary at design scale (partial indexes already exclude archived rows from
  hot-path cost) but made trivial by the relational short-term tier if the record
  ever reaches millions of rows.

## 6. Open Questions

- **OQ-1** Facade serialization details — exact metadata key layout and versioning
  (`schema_version` key) — settled at ARCHITECTURE time.
- **OQ-2** Whether the review queue lives as AGE data or a small relational table
  beside the graph — settled at ARCHITECTURE time against semantica's actual store
  shapes. (The NFR-4 tier-split direction — relational short-term — strongly implies
  a relational queue and transitions table; confirm when the schema is drawn.)
- **OQ-3** Embedding model choice for precedent search (wired via the `Embedder`
  port; candidate: whatever meridian standardizes on) — deferred until meridian
  exists; a local/stub embedder suffices for development.
