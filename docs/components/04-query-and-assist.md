# Component: Query Service and Assist Layer

## Purpose

Everything that reads the record (REQUIREMENTS FR-6) and everything that helps
curate it without authority (FR-7): relevance, history, precedent, feeds,
projections, export — plus the embedding backfill, discovery, and archival
sweeps behind the `Embedder`/`Suggester` ports.

## Owns

- Query composition over store reads (the store executes; this layer shapes).
- The precedent pipeline (embed question → k-NN → filters → hydrate).
- The three sweeps (backfill, discovery, archival) as host-invoked library calls.
- Projection shaping (`full` vs `compact`) and export assembly.

## Depends on

Store port; `Embedder` and `Suggester` ports (ARCHITECTURE §3.1); Lifecycle
Engine only for the archival sweep's transitions. Core stays LLM-free (FR-7.1).

## Query contracts (FR-6)

- **relevant()** — filters: domains (default all), status set (default
  `current`), tier, subject (returns subject-match OR unscoped, FR-6.1), `as_of`
  over `valid_from/until`, lexical `text` filter, `include_archived`. Projection
  `compact` (id, domain, tier, status, outcome one-liner, subject refs) or
  `full`. Deterministic ordering (recency, then id).
- **history()** — the full record of one decision: content, refs, transitions in
  order, links, and both chains (predecessors/successors via recursive CTE) plus
  supplements. Includes archived/discarded targets (history hides nothing).
- **precedent()** — `Embedder.embed(question)` → HNSW k-NN over non-archived
  embeddings (both tiers) → attribute filters → hydrate projections, each with
  its similarity score and status (superseded/`not_promoted` history INCLUDED by
  default — labeled, not hidden; FR-6.3).
- **changes()** — transitions by window/action/actor, joined to compact
  projections (FR-6.5).
- **queue()** — open items with recommender, rationale, confidence, age;
  orderings `oldest` | `shakiest` (confidence asc) | `domain`.
- **get_many() / by_source()** — batch direct access (FR-6.8).
- **export()** — filtered decisions with their refs, links, transitions as one
  JSON document (schema_version stamped; FR-6.6).

## The sweeps (host-scheduled; each idempotent and bounded)

- **backfill_embeddings(batch)** — FR-6.9 backlog → `Embedder.embed` in batches
  → upsert `embeddings`. Embedded text = `scenario + outcome + reasoning`
  (OQ-3). Failure leaves the backlog intact (I-5: recall degrades, nothing
  breaks).
- **discover(batch)** — per newly embedded decision: k-NN (k ≤ config, default
  10) → structural filters (same domain, subject overlap, temporal order,
  status compatibility; FR-7.4) → `Suggester.classify_pairs` → queue items with
  rationale+confidence, floor-filtered and per-sweep capped. Also
  `Suggester.assess_promotion` over aging unrecommended short_term decisions →
  pending-promote items (`proposed_by engine:binnacle`). No `Suggester`
  configured → sweep no-ops cleanly.
- **archive_stale()** — FR-3.4 clock rule → bulk `archived` transitions via the
  Lifecycle Engine.

## Contract points

- Sweeps never mutate outside queue/embeddings/archival-transitions (I-4/I-5).
- Discovery work is O(k) per new decision; the implementation MUST NOT contain
  any all-pairs path (FR-7.4) — enforced by an explicit test seeding N decisions
  and asserting Suggester call count ≤ N·k.
- Compact projections are SQL-level (no full-row fetch then trim) — FR-6.7's
  token-budget purpose is defeated by client-side trimming of a heavy read.
- Precedent similarity scores are surfaced, never thresholded silently.

## Acceptance

- Relevance matrix test: scoped/unscoped × domain × status × as_of × archived
  grid against a seeded fixture (§7 narrative's examples as fixtures).
- Precedent test with the stub embedder: known-nearest fixtures return in score
  order; superseded ancestors present and labeled.
- Sweep tests: backfill idempotency; discovery cap/floor honored, call-count
  bound asserted; archival only touches clock-eligible rows; all three no-op
  cleanly on empty input.
- Export round-trip: export → JSON schema check → spot re-hydration equality.
