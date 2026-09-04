# OVERVIEW

System-level context for the binnacle monorepo — how its packages relate,
and the tooling decisions that hold the workspace together. Each package's
own functional/non-functional requirements and internal architecture live
in `docs/<package>/REQUIREMENTS.md` and `docs/<package>/ARCHITECTURE.md`;
this document is the layer above those, not a replacement for any of them.

## 1. Packages

| Package | Role |
|---|---|
| **binnacle-core** | The decision-record library: domain model, lifecycle engine, PostgreSQL/pgvector store. See `docs/binnacle-core/`. |
| **binnacle-router** | A library (not a service) exposing binnacle-core's functionality as REST + MCP, so a host process — a standalone binnacle deployment, or another service such as Meridian — can mount it. See `docs/binnacle-router/`. |
| **binnacle-ui** | JS/TypeScript UI components (review queue, decision history, precedent search, etc.), usable by any consumer that embeds binnacle. See `docs/binnacle-ui/`. |

`binnacle-service` (a runnable daemon composing `binnacle-core` +
`binnacle-router`) was considered and explicitly deferred — see
`docs/superpowers/specs/2026-09-04-monorepo-restructure-design.md` §2. It
would directly conflict with `binnacle-core`'s FR-8.1 ("library, not
authority — no daemon") and has no concrete use case yet.

## 2. Repository layout

Flat `packages/` directory regardless of language — this matches what both
`uv` workspaces and `pnpm` expect by default:

```
packages/
  binnacle-core/     Python, dist name "binnacle-core", import name binnacle_core
  binnacle-router/   Python, dist name "binnacle-router", import name binnacle_router
  binnacle-ui/       TypeScript, package name "binnacle-ui"
```

The package directory name, the distribution name, and the docs directory
name (`docs/<name>/`) are always identical, hyphenated. Python's inability
to have hyphens in import identifiers is the one unavoidable divergence
(the underscored equivalent is the import name).

## 3. Workspace tooling

Two workspace managers, one repository, each blind to the other:

- **Python** (`binnacle-core`, `binnacle-router`): the root `pyproject.toml`
  is a `uv` workspace manifest (`[tool.uv.workspace]`) with an explicit
  member list, never a glob — `uv` requires every glob-matched member to
  contain a `pyproject.toml`, which `binnacle-ui` does not.
- **JS** (`binnacle-ui`): a root `pnpm-workspace.yaml`.
- `[tool.ruff]` stays shared at the root `pyproject.toml` — Ruff skips any
  `pyproject.toml` lacking a `[tool.ruff]` table and keeps walking up, so
  neither Python package's own `pyproject.toml` may add its own
  `[tool.ruff]` section without silently opting out of the shared style.
- `[tool.importlinter]` is per-package (internal layering is a per-package
  concern).

## 4. Cross-package dependencies

`binnacle-router` will eventually depend on `binnacle-core`. Which part of
`binnacle-core`'s surface it may depend on (its public application-layer
client vs. reaching into internals) is `binnacle-router`'s own future
spec's responsibility to define and enforce via an import-linter contract
— not decided here, and not decided implicitly by omission either.

## 5. Quality gates

- **`pre-commit` stage** (every commit, fast, no I/O): gitleaks, Ruff
  (lint + format), Biome (lint + format) for `binnacle-ui`.
- **`pre-push` stage** (once, before code leaves the machine): the same
  `scripts/check.sh` CI runs — mypy and import-linter for both Python
  packages, the full test suite (unit + integration) for `binnacle-core`
  and `binnacle-router`, and lint/typecheck/test/build for `binnacle-ui`.
- **CI**: runs `scripts/check.sh` directly, plus
  `pre-commit run --all-files --hook-stage push` to exercise the hook
  configuration itself.

Full rationale for each of these decisions lives in
`docs/superpowers/specs/2026-09-04-monorepo-restructure-design.md`.
