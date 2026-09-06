# Changelog

All notable changes to `binnacle-router` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/) and
[SemVer](https://semver.org/).

## [Unreleased]

### Changed

- `__version__` is read from the installed distribution's metadata rather
  than restated in `__init__.py`, so `pyproject.toml` is the single place
  a version is declared and the two cannot drift.

## [0.2.0] - 2026-09-05

### Added

- A mountable `fastapi.APIRouter` (`make_router(binnacle, get_actor)`)
  exposing `binnacle-core` as REST, plus `install_error_handlers(app)`
  mapping every typed core error to an RFC 7807 problem document. ~30
  endpoints across decision reads and writes, the review queue, domain
  registry and dashboard summaries, the changes/precedent/export feeds,
  and the three engine sweeps — see `docs/binnacle-router/REQUIREMENTS.md`
  FR-3 for the full catalog and `README.md` for the mounting recipe.
  `install_error_handlers` registers only classes a host route does not
  raise; the `ValueError`/`TypeError` -> 422 mapping is scoped to this
  package's own routes by a `route_class` (FR-5.5), so mounting binnacle
  does not change how the host's own endpoints fail. Every operation's 422
  is published as `application/problem+json` carrying a `ProblemDocument`
  schema, not FastAPI's stock `HTTPValidationError` (FR-5.6).
- An import-linter contract restricting this package to `binnacle-core`'s
  public surface (`packages/binnacle-router/pyproject.toml`
  `[tool.importlinter]`), resolving the dependency-boundary question
  `docs/OVERVIEW.md` §4 had left open.

### Fixed

- `limit`/`batch` on every paginated or batched endpoint (`GET /decisions`,
  `GET /decisions/by_source`, `GET /queue`, `GET /changes`,
  `GET /precedent`, `POST /sweeps:backfill_embeddings`,
  `POST /sweeps:discover`) now reject `0` and negative values with a 422
  problem document instead of reaching `binnacle-core` unchecked.
  `limit=-5` previously surfaced as an unmapped 500 from PostgreSQL;
  `limit=0` previously returned an empty page together with a
  `next_cursor`, looping a client that pages on it forever. The upper
  bound remains an open API-policy question — see the README's "Known
  gaps" section.
