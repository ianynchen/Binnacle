# ADR 0001: Restructure into a monorepo (binnacle-core, binnacle-router, binnacle-ui)

Status: Accepted
Date: 2026-09-04

## Context

Binnacle was a single Python package. Three additional pieces were wanted:
a library exposing binnacle's functionality as REST + MCP
(`binnacle-router`), a set of JS UI components usable by any consumer that
embeds binnacle (`binnacle-ui`), and — considered, then explicitly
deferred — a standalone runnable service composing the two
(`binnacle-service`).

## Decision

Restructure the repository into a `uv`/`pnpm` monorepo:

- `packages/binnacle-core` — today's `src/binnacle`, moved and renamed
  (distribution `binnacle-core`, import `binnacle_core`).
- `packages/binnacle-router` — scaffolded empty; its REST/MCP surface is
  future work.
- `packages/binnacle-ui` — scaffolded empty (TypeScript + React + Biome +
  Vitest); its component set is future work.
- `binnacle-service` is **not** built — it would conflict with
  `binnacle-core`'s FR-8.1 ("library, not authority — no daemon") for a
  standalone-use-case that doesn't yet concretely exist.

Documentation splits per package (`docs/<package>/REQUIREMENTS.md`,
`docs/<package>/ARCHITECTURE.md`), with a new `docs/OVERVIEW.md` holding
system-level, cross-package context. `CHANGELOG.md` becomes one per
package. Quality gates split across `pre-commit` (fast: gitleaks, Ruff,
Biome) and `pre-push` (full: the same `scripts/check.sh` CI runs), so
what's pushed is guaranteed to be what CI checks.

## Consequences

- **Breaking change**: `import binnacle` no longer works; callers must
  use `import binnacle_core` and depend on the `binnacle-core` distribution
  name. `binnacle-core` bumped to `0.3.0` — a minor version bump with a `BREAKING CHANGE:` footer, per GUIDELINES §11's pre-1.0 allowance for a breaking change to ride a minor rather than forcing a major version.
- `binnacle-router`'s and `binnacle-ui`'s functional design, and the
  import-linter contract governing how `binnacle-router` may depend on
  `binnacle-core`, are explicitly deferred to their own future
  spec/plan cycles.
- The convention this ADR establishes (`docs/adr/NNNN-<topic>.md`, plain
  numbered Markdown, immutable once accepted — a reversal is a new,
  superseding ADR) is not yet written into GUIDELINES §5.2's own text;
  that's a deferred follow-up, not part of this change.

Full design rationale, rejected alternatives, and decision records (DR-1
through DR-7):
`docs/superpowers/specs/2026-09-04-monorepo-restructure-design.md`.
