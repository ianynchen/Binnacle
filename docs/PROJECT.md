# PROJECT

Delivery status across the binnacle monorepo's packages. Status values:
`planned | in-progress | delivered | deferred | cancelled`. Each entry links
to the requirement it satisfies and names its package — see GUIDELINES §5.
This file is created for the first time alongside the query-additions work
(`docs/superpowers/plans/2026-09-05-binnacle-core-query-additions.md`);
statuses below reflect what is verified in `src/` as of that work landing
(GUIDELINES §5.3), not the plans that preceded it.

## binnacle-core

| Entry | Status | Requirement |
|---|---|---|
| Recording, domain registry, lifecycle/transitions/audit, promotion and review queue, relationships between decisions | delivered | `docs/binnacle-core/REQUIREMENTS.md` FR-1–FR-5 |
| Assist layer ports (`Suggester`, `Embedder`) and discovery pipeline | delivered | FR-7 |
| Packaging as a library (config-object init, async-first, house layering) | delivered | FR-8 |
| Query surface baseline: relevance, history, precedent, queue reads, changes feed, export, projections, direct access, candidate enumeration | delivered | FR-6.1–FR-6.9 |
| Query additions: `sort`/`order`/`after` pagination and `evidence`/`expiring_before` filters on `relevant()`; pagination on `queue()`; `relevant_count()`, `queue_summary()`, `domain_summary()`; `after_id` tiebreaker on `changes()` — **breaking:** `relevant()`/`queue()` now return `Page[...]` | delivered | FR-6.1, FR-6.4, FR-6.5, FR-6.10; `packages/binnacle-core/CHANGELOG.md` `[Unreleased]` |
| Evidence-ref partial index (`idx_refs_evidence`, migration `0004_evidence_ref_index`) | delivered | `docs/binnacle-core/ARCHITECTURE.md` §4 |
| NFR-7 performance targets, verified by seeded perf test at design scale (10,000 decisions / 100,000 transitions) | delivered | NFR-7 |

## binnacle-router

| Entry | Status | Requirement |
|---|---|---|
| REST surface: `make_router()` factory, `install_error_handlers()` RFC 7807 mapping, decisions (read + write), queue, domain registry, dashboard summaries, changes/precedent/export feeds, engine sweeps | delivered | `docs/binnacle-router/REQUIREMENTS.md` FR-1–FR-7; `packages/binnacle-router/CHANGELOG.md` `[Unreleased]` |
| Import-linter contract restricting the package to `binnacle-core`'s public surface | delivered | `docs/binnacle-router/ARCHITECTURE.md` §5 |
| Full per-operation OpenAPI response catalog (400/403/404/409/500) | deferred | `docs/binnacle-router/REQUIREMENTS.md` §5; the 422 declaration is delivered per FR-5.6 |
| MCP surface | deferred | `docs/binnacle-router/REQUIREMENTS.md` §5 |

## binnacle-ui

| Entry | Status | Requirement |
|---|---|---|
| Package scaffold (one placeholder component, no functional design yet) | planned | `docs/binnacle-ui/REQUIREMENTS.md` |
