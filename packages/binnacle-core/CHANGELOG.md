# Changelog

All notable changes to `binnacle-core` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/) and
[SemVer](https://semver.org/).

## [Unreleased]

### Added

- **Partial replacement between decisions** (FR-3.1a/3.1b/3.1c,
  [ADR 0002](../../docs/adr/0002-whole-and-partial-replacement.md)):
  `Binnacle.partially_supersede(new_id, old_id, part, actor)` records that a
  later decision replaced only SOME of an earlier decision's rulings. It links
  the two with the new `PARTIALLY_SUPERSEDES` kind (carrying `part`, prose
  naming what was replaced) and appends `partial_replacement_linked`
  transitions, changing no status: the target remains the authority for every
  ruling the replacer left in force, so it stays `current` and stays in
  current-status reads. Previously the only way to record such a relationship
  was `supersede`, which asserts that the whole decision has fallen.
- `links.part` and the `links_part_iff_partial` CHECK constraint (migration
  `0005_link_part`), so a partial-replacement link with no part is refused by
  the database, not only by the Lifecycle Engine.
- `HistoryRecord.partial_replacers` — each partial replacer paired with the
  part it replaced (new public type `PartialReplacement`). `Link` gains `part`
  (`None` for every other kind), so `history()` and `export()` carry it.
  Supersession chains (`predecessors`/`successors`) stay whole-replacement
  only, so a caller can still ask "is anything here in force?" truthfully.
- `StorePort.links_between(tx, a, b)`, a tx-scoped read of the links between
  two decisions, for the refusal checks below.

### Changed

- `supersede` (and `apply_item`/`resolve_conflict`'s winner path) now raise
  `InvalidTransition` when the pair already carries a partial-replacement link
  — the whole-and-partial refusal applies in either order of recording.
- `StorePort.insert_link` takes an optional keyword-only `part`. Existing call
  sites are unaffected.

## [0.5.0] - 2026-09-05

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
