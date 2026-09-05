# Changelog

All notable changes to `binnacle-core` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/) and
[SemVer](https://semver.org/).

## [Unreleased]

### Added

- `Tier`, `PrecedentHit`, `BackfillSummary`, `DiscoverySummary`, and
  `ArchivalSummary` are re-exported from the top-level package. They are
  named by public method signatures, so callers restricted to the public
  surface previously could not annotate what those methods return.
- A `py.typed` marker, so consumers' type checkers treat this package's
  types as real rather than `Any`.

## [0.4.0] - 2026-09-05

### Added

- Sortable ordering on `relevant()` (`decided_at`, `recorded_at`,
  `last_touched_at`, `valid_until`), plus `evidence` and `expiring_before`
  filters.
- Keyset pagination on `relevant()` and `queue()` via opaque cursors.
- `relevant_count()`, `queue_summary()`, `domain_summary()`.
- `after_id` tiebreaker on `changes()`.
- New public types `Page` and `DomainSummary`, and new typed errors
  `InvalidCursor` (a malformed cursor, or one replayed under a different
  ordering) and `InvalidSort` (an unrecognized sort key) — both raised
  rather than allowed to escape as a bare `ValueError`/`KeyError`.
- Migration `0004_evidence_ref_index`, a partial index backing the
  `evidence` filter. Hosts must run `migrate()` to pick it up; a rollback
  step ships with it.

### Changed

- **Breaking:** `relevant()` and `queue()` return `Page[...]` rather than bare
  lists. An input-only cursor cannot support the derived `last_touched_at`
  sort key, since callers cannot see a derived value in returned rows.
  `queue()` also gains a default `limit=50` where it previously returned
  every open item unbounded — a mechanical `.queue()` → `.queue().items`
  migration type-checks cleanly but silently drops every item past the
  50th; pass an explicit `limit` (and page via `after`) to preserve seeing
  everything.

## [0.3.0] - 2026-09-04

### Changed

- **Breaking:** package renamed from `binnacle` to `binnacle-core`
  (import name `binnacle` → `binnacle_core`) as part of splitting the
  repo into a monorepo (`packages/binnacle-core`, `packages/binnacle-router`,
  `packages/binnacle-ui`). See
  `docs/adr/0001-monorepo-restructure.md`. No logic change.
