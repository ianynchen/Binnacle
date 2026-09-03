# Component: Configuration and Client

## Purpose

The public face of the library (REQUIREMENTS FR-1, FR-4, FR-6, FR-8). Defines the
config object an embedder constructs, and the client API every caller — meridian's
UI/API/MCP surface, its sweep jobs, and its agent tools — programs against.
Everything else in the package is reachable only through this surface.

## Owns

- `BinnacleConfig` and sub-models; construction-time validation.
- The `Binnacle` client class: recording, lifecycle verbs, queue, queries, export,
  `migrate()`.
- Actor attestation at the boundary (typed `Actor`, ARCHITECTURE I-2).

## Depends on

`application` (recorder, lifecycle, queue, query, discovery, archival, export,
ports). Imports no DB driver directly (ARCHITECTURE layering).

## Configuration object

Library rules match tradewind's (no env/file/global reads; multiple instances;
fail at construction):

```python
class BinnacleConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    dsn: str | None = None  # or pool=<caller-supplied psycopg pool>; exactly one
    pool: Any | None = None
    schema_name: str = "binnacle"  # ARCHITECTURE §4.1
    embedder: Embedder  # live port (nomic adapter in meridian; stub in tests)
    suggester: Suggester | None = None  # live port; None disables discovery classification
    embedding_dim: int = 768  # must match the migrated VECTOR(n) column
    archival_age_days: int = 90  # FR-3.4
    compact_outcome_chars: int = 200  # FR-6.7 compact-projection truncation
    discovery: DiscoveryConfig = DiscoveryConfig()  # k (≤10), confidence_floor, per_sweep_cap
```

## Client API (async-first)

```python
bn = Binnacle(config)                       # validates; no I/O
await bn.migrate()                          # host-invoked, yoyo-backed (§4.1); never implicit
await bn.aclose()

# Recording (FR-1): actor is attested by the caller (meridian) — kind:id
d = await bn.record(NewDecision(...), actor=Actor("agent", "meridian/sess-1"))
d = await bn.record(NewDecision(..., decision_id=my_uuid))       # idempotent (FR-1.6)
d = await bn.record_long_term(NewDecision(...), actor=human)     # FR-4.4, human kind enforced

# Lifecycle (FR-3/4/5) — all verbs delegate to the Lifecycle Engine:
await bn.recommend(decision_id, actor, reason)                   # any actor → queue
await bn.promote(item_id, actor=human)                           # verbatim
await bn.promote_refined(source_ids, refined=NewDecision(...), actor=human) # FR-4.6, ≥1 sources;
                                                                 # open queue items for those sources auto-resolve
await bn.decline(item_id, actor=human, reason=...)               # → not_promoted
await bn.discard(decision_id, actor, reason)                     # FR-3.3 permission rule
await bn.supersede(new_id, old_id, actor)                        # I-2 gate applies to long_term olds
await bn.supplement(new_id, old_id, actor)                       # same gate
await bn.reactivate(decision_id, actor)                          # un-archive (FR-3.4)
await bn.apply_item(item_id, actor)                              # execute a suggested link/supersede
                                                                 #   item (human if LT target)
await bn.dismiss_item(item_id, actor, reason)                    # negative resolution, any item kind

# Queries (FR-6):
await bn.relevant(domains=None, subject=None, status=("current",), tier=None,
                  as_of=None, text=None, projection="compact", limit=50,
                  include_archived=False)
await bn.history(decision_id)                # content + transitions + links + chains (FR-6.2)
await bn.precedent(question, domains=None, tiers=None, limit=10, include_dead=True)
await bn.queue(kinds=None, order="oldest")   # FR-4.3 / FR-6.4
await bn.changes(since, actions=None, actor=None, limit=500)     # FR-6.5
await bn.get_many(ids) / bn.by_source(source, ...)               # FR-6.8
await bn.export(filter) -> JSON              # FR-6.6

# Sweeps (host-scheduled, FR-7):
await bn.backfill_embeddings(batch=...)      # drains FR-6.9's backlog via Embedder
await bn.discover(batch=...)                 # candidates → Suggester → queue items
await bn.archive_stale()                     # FR-3.4
```

## Contract points

- Every verb takes an explicit `Actor(kind, id)`; the client validates shape, the
  Lifecycle Engine enforces authority (I-2). Binnacle never guesses a kind.
- `record()` never awaits the `Embedder` (I-5); `record_long_term`/`promote*`
  refuse non-human actors with a typed error.
- All ids are UUIDs, validated at the boundary; unknown domain → typed error
  naming the registry (FR-2.1).
- Registry management: `bn.domains()` / `bn.add_domain(name, desc, actor=human)`
  / `bn.update_domain(name, desc, actor=human)` / `bn.deactivate_domain(...)` —
  human-only, audited in `domain_transitions` (FR-2.2).
- Verbatim `promote` takes a queue item (everything durable passes through the
  queue); a human promoting an unrecommended decision either self-recommends
  first or uses `promote_refined`/`record_long_term` — deliberate, documented
  ergonomics.
- Errors are a typed hierarchy (`BinnacleError` root): `UnknownDomain`,
  `DecisionNotFound`, `InvalidTransition`, `AuthorityViolation`,
  `IdempotencyConflict` (FR-1.6), `EmbeddingDimensionMismatch`, `ConfigError`.

## Acceptance

- Construction matrix: dsn-xor-pool enforced; bad discovery caps rejected; two
  instances with different schemas coexist against one database.
- An embedder-shaped test drives record → recommend → promote_refined → query →
  export end-to-end through the public API only, with a stub embedder/suggester.
