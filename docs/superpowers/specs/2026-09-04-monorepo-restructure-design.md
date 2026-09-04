# Monorepo restructure — design spec

Status: proposed (pending user review)
Author: Yining Chen, with Claude Sonnet 5
Date: 2026-09-04

## 1. Context and goals

Binnacle today is a single Python package (`src/binnacle`) implementing the
decision-record core described in `docs/REQUIREMENTS.md` /
`docs/ARCHITECTURE.md`. This spec restructures the repository into a monorepo
that will eventually host three packages:

1. **`binnacle-core`** — the existing logic + PostgreSQL/pgvector schema
   (today's `src/binnacle`, moved and renamed).
2. **`binnacle-router`** — a library (not a service) exposing binnacle
   functionality as REST + MCP, so a host process (a standalone binnacle
   deployment, or another service such as Meridian) can mount it.
3. **`binnacle-ui`** — a single JS/TypeScript package of UI components
   (review queue, decision history, precedent search, etc.) usable by any
   consumer that embeds binnacle, including a future service or Meridian.

A fourth package, `binnacle-service` (a runnable daemon composing
`binnacle-core` + `binnacle-router`), was considered and **explicitly
deferred** — see §2.

This spec covers **only the restructuring**: repository layout, naming,
documentation layout, tooling, and quality gates. It scaffolds
`binnacle-router` and `binnacle-ui` as empty, wired-up packages; it does not
design their internal functionality. Each gets its own future
spec/plan cycle once scaffolded.

## 2. Non-goals

- **`binnacle-service` is out of scope.** The motivation (running binnacle
  fully standalone in the future) is real but speculative today, and adding a
  daemon is a direct conflict with the currently-documented FR-8.1 ("library,
  not authority — no daemon"). Revisit as its own architectural decision, with
  its own ADR, when a concrete standalone use case exists.
- **No functional design for `binnacle-router` or `binnacle-ui`.** Their
  REST/MCP surface and component set are future work.
- **No behavior change to `binnacle-core`'s logic.** This is a structural
  move: package rename, directory move, tooling wiring. The domain model,
  schema, and application logic are unchanged. **This does not mean the
  change is invisible externally**: renaming the distribution from
  `binnacle` to `binnacle-core` and the import from `binnacle` to
  `binnacle_core` breaks any existing `pip install binnacle` / `import
  binnacle` — see DR-6.
- **No cross-package dependency/layering design.** `binnacle-router` will
  eventually depend on `binnacle-core`, but how it may do so (e.g. "only
  through `binnacle-core`'s public application-layer surface, never its
  adapters") is deliberately left to `binnacle-router`'s own future spec —
  see DR-7. This spec does not wire that dependency yet, since the router
  package is scaffolded empty.

## 3. Package layout

Flat `packages/` directory, regardless of language — this matches what both
`uv` workspaces and JS workspace managers (pnpm) expect by default, and reads
as "one product, three components" rather than splitting by ecosystem first.

```
packages/
  binnacle-core/
    pyproject.toml       (dist name "binnacle-core")
    src/binnacle_core/   (domain/, application/, adapters/, migrations/ — moved as-is from src/binnacle/)
    tests/               (unit/, architecture/, db/ — moved as-is from tests/)
    CHANGELOG.md
  binnacle-router/
    pyproject.toml       (dist name "binnacle-router")
    src/binnacle_router/
    tests/
    CHANGELOG.md
  binnacle-ui/
    package.json         (name "binnacle-ui")
    src/
    CHANGELOG.md
```

`binnacle-router` and `binnacle-ui` are scaffolded empty (minimal package
manifest, one placeholder module, one placeholder test) — enough for the
workspace and CI wiring to have something real to operate on.

## 4. Naming convention

**The package directory name, the distribution name, and the docs directory
name are always identical**, hyphenated: `binnacle-core`, `binnacle-router`,
`binnacle-ui`. This replaces today's mismatch (directory/import name
`binnacle` for what is conceptually "the core").

Python cannot have hyphens in import identifiers, so the one unavoidable
divergence is the importable module name, which uses the underscored
equivalent of the distribution name:

| Distribution / directory name | Python import name |
|---|---|
| `binnacle-core` | `binnacle_core` |
| `binnacle-router` | `binnacle_router` |

`binnacle-ui` has no such split — JS package names and import specifiers are
the same string.

## 5. Documentation layout

```
GUIDELINES.md, CLAUDE.md, AGENTS.md, README.md   (repo root, unchanged — process
                                                    docs apply uniformly across packages)

docs/
  OVERVIEW.md              (NEW — system-level C4 L1 across packages: how binnacle-core,
                             binnacle-router, and binnacle-ui relate; monorepo layout and
                             tooling decisions. Replaces the system-context portion of
                             today's docs/ARCHITECTURE.md.)
  PROJECT.md               (single, cross-package delivery tracker; each entry names its
                             package and links a requirement)
  RUNBOOK.md                (single, cross-package lessons-learned)
  adr/
    0001-monorepo-restructure.md   (this change, once approved and implemented)
  superpowers/
    specs/YYYY-MM-DD-*.md   (flat, topic-named — many specs are cross-cutting, like this one)
    plans/YYYY-MM-DD-*.md

  binnacle-core/
    REQUIREMENTS.md         (today's docs/REQUIREMENTS.md, moved as-is)
    ARCHITECTURE.md         (today's docs/ARCHITECTURE.md, moved — now scoped to this
                             package's L2/L3; system-level L1 content moves to OVERVIEW.md)
    components/*            (today's docs/components/*, moved as-is)
  binnacle-router/
    REQUIREMENTS.md         (skeleton: states the package exists and its role, defers
                             functional requirements to its own future spec)
    ARCHITECTURE.md         (skeleton, same treatment)
  binnacle-ui/
    REQUIREMENTS.md         (skeleton, same treatment)
    ARCHITECTURE.md         (skeleton, same treatment)
```

`CHANGELOG.md` moves from repo root to **one per package**
(`packages/<name>/CHANGELOG.md`), living next to that package's own
`pyproject.toml`/`package.json`, since each package now carries its own
independent SemVer version (GUIDELINES §11) and a single interleaved
root changelog stops being meaningful once versions diverge.

### 5.1 ADRs

`docs/adr/NNNN-<topic>.md`, plain numbered Markdown, immutable once accepted
— a later reversal is a new, superseding ADR, never an edit to an old one.
This is a new convention: GUIDELINES §5.2 has required ADRs since it was
written but never specified where they live or what format they take. This
spec decides that (here) and uses it immediately for its own ADR (§10 step
9) — but **writing that decision into GUIDELINES §5.2's own text is
deliberately deferred to a follow-up after the restructuring lands** (per
explicit direction), not bundled into this migration. Deliberately **not**
stored as records inside a running binnacle instance: `binnacle-core` itself
is one of the things this ADR describes restructuring, so recording it there
would be circular.

### 5.2 GUIDELINES.md updates required

`§1.1`, `§5`, and `§5.1` reference `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md`,
and `docs/components/*` as flat, singular paths. These become per-package
(`docs/<package>/REQUIREMENTS.md`, etc.), with `docs/OVERVIEW.md` added as
the system-level document sitting above them. `§11` (Versioning /
Definition of Done) gets a note that `CHANGELOG.md` is per-package.

**Not included in this migration:** writing the `docs/adr/` location and
format (§5.1) into `§5.2`'s own text. `§5.2` still mandates ADRs without
saying where they go after this change lands — that gap is real and this
spec's own ADR (§10 step 9) uses the location anyway, but closing the gap in
GUIDELINES' text is explicitly deferred to a follow-up after the
restructuring, not bundled into it.

## 6. Workspace tooling

Two workspace managers, one repository, each blind to the other:

- **Python** (`binnacle-core`, `binnacle-router`): the root `pyproject.toml`
  becomes a `uv` workspace manifest (`[tool.uv.workspace]`) with an
  **explicit** member list — `members = ["packages/binnacle-core",
  "packages/binnacle-router"]`, not a `packages/*` glob. This isn't just
  tidiness: `uv` requires every glob-matched member to contain a
  `pyproject.toml`, so a `packages/*` glob would hard-error on
  `binnacle-ui` (verified against `uv`'s workspace docs). Each package
  keeps its own `[project]` block, dependencies, and version.
- **JS** (`binnacle-ui`): a root `pnpm-workspace.yaml` covering
  `packages/binnacle-ui`.
- `[tool.importlinter]` (currently root-level, `root_package = "binnacle"`)
  moves into `packages/binnacle-core/pyproject.toml` as `root_package =
  "binnacle_core"` — layering enforcement is internal to a package.
- `[tool.ruff]` stays shared at the root `pyproject.toml`: ruff skips any
  `pyproject.toml` that lacks a `[tool.ruff]` table and keeps walking up the
  directory tree, so as long as neither package's own `pyproject.toml` adds
  its own `[tool.ruff]` section, both share the root config with zero
  duplication (verified against Ruff's config-discovery docs during review,
  not assumed).

## 7. binnacle-ui technology choices

- **TypeScript**, not plain JS. Not explicitly requested, but flagged here as
  a deliberate decision for review: it matches the strict-typing culture
  already established for the Python side (mypy strict) and costs nothing
  extra on a brand-new package with no existing JS to migrate.
- **React** — confirmed in prior discussion (no existing consumer stack to
  match against; React has the broadest ecosystem for an embeddable
  component library).
- **Biome** for lint + format — one tool, one config, instead of
  ESLint+Prettier's two. Trade-off accepted: Biome's plugin ecosystem is
  thinner (notably around `jsx-a11y`-style accessibility rules), but it
  covers the common cases and ESLint can be added later for a specific gap
  without conflicting with Biome.
- **Vitest** for unit tests — the standard pairing for a new React project.
- Bundler/build tooling for shipping the package (e.g. `tsup` vs. Vite
  library mode) is left to the implementation plan — it doesn't affect the
  repository structure or naming decided here.

## 8. Quality gates

The guiding requirement: **what's pushed must be verified by the same gate
CI runs**, so "it passed my push" and "it'll pass CI" are the same statement.
This is achieved by using `pre-commit`'s two hook stages rather than a
fast/thorough split between local and CI:

- **`pre-commit` stage** (every commit, must stay fast, no I/O):
  gitleaks (already present, unchanged), `ruff` + `ruff-format` (unchanged),
  Biome lint+format for `binnacle-ui`.
- **`pre-push` stage** (once, before code leaves the machine): a single hook
  invoking `bash scripts/check.sh` directly — the same script, same command,
  CI's "Run gates" step calls. Covers mypy and import-linter for both Python
  packages, the **full** test suite (unit + `tests/db`, which needs a live
  Postgres+pgvector) for `binnacle-core` and `binnacle-router`, and
  lint/typecheck/test/build for `binnacle-ui`.
- **CI**: runs `scripts/check.sh` directly (the authoritative full gate,
  extended to loop over both Python packages plus the JS package), and
  separately runs `pre-commit run --all-files --hook-stage push` (exercises
  the hook *configuration* itself, so a broken `.pre-commit-config.yaml`
  fails CI too) — mirroring today's existing two-step CI pattern.

Whether `scripts/check.sh` should spin up its own ephemeral Postgres
container when `BINNACLE_TEST_DSN` isn't set (so `git push` "just works"
without manual DB setup) is left to the implementation plan — it's a
convenience detail, not a structural one.

**A real gap this design must not paper over:** `pre-commit install` only
wires the `pre-commit` stage by default — it does **not** install `pre-push`
hooks unless run as `pre-commit install --hook-type pre-commit --hook-type
pre-push`. If that's left as tribal knowledge, the `pre-push` stage silently
never runs for anyone who followed the "normal" setup instructions, and
DR-5's entire "what's pushed is what CI checks" guarantee becomes false
without anyone noticing. This has to be a documented (or better, scripted —
e.g. a `make setup` / `scripts/dev-setup.sh` that runs both installs)
contributor setup step, not something left implicit. Captured as a required
step in the migration outline (§10) rather than left to be rediscovered.

## 9. Decision records

- **DR-1 Flat `packages/`, not split-by-ecosystem.** Rejected splitting into
  `python/` and `js/` top-level directories: with only one JS package against
  two Python ones, that split buys isolation not yet needed and costs the
  "one coherent product" framing. Flat `packages/*` also matches what `uv`
  and pnpm both expect natively.
- **DR-2 `binnacle-service` deferred, not built speculatively.** Building a
  daemon today for a standalone-use-case that doesn't yet exist would violate
  YAGNI (GUIDELINES §2) and force an immediate, unforced contradiction of
  FR-8.1. Revisit with its own ADR when the use case is concrete.
- **DR-3 Docs split by package, process docs stay shared.** REQUIREMENTS and
  ARCHITECTURE are contracts specific to one package's behavior (router's FRs
  share nothing with core's DB perf targets); splitting them prevents the
  kind of "cite it as shipped without checking" drift GUIDELINES §5.3 warns
  against, since a single combined document would be too large to hold in
  mind. PROJECT.md, RUNBOOK.md, and specs/plans are process/history
  artifacts, not contracts, and stay shared for one cross-package view.
- **DR-4 CHANGELOG.md per package.** Forced by independent per-package
  SemVer versions (GUIDELINES §11) — a single root changelog interleaving
  unrelated version bumps across three independently-versioned packages
  stops being useful once they diverge.
- **DR-5 `pre-push`, not a fast/thorough split, reconciles "same gate
  everywhere" with "don't slow down every commit."** The alternative (running
  the full suite, including live-Postgres `tests/db`, on every commit) would
  either block committing when Postgres isn't running locally, or be slow
  enough to discourage committing at all. `pre-push` runs once, right before
  the point that matters (code leaving the machine), running the literal same
  script CI runs.
- **DR-6 The rename is a breaking change, called out explicitly rather than
  absorbed silently into "just a move."** `binnacle` → `binnacle-core`
  changes the distribution name and the top-level import (`import binnacle`
  stops working). GUIDELINES §11 requires proposing the exact SemVer bump and
  getting explicit confirmation before applying it — this spec does not
  pre-decide that number, but flags that "no behavior change" (§2) is true of
  the *logic*, not of the *public import surface*, and the implementation
  plan must not let that distinction get lost.
- **DR-7 Cross-package dependency direction (router → core) is deferred, not
  silently skipped.** GUIDELINES §8 requires architecture rules to be
  enforced, not aspirational — so when `binnacle-router` gains real code, its
  own spec must define and enforce (via an import-linter contract or
  equivalent) which part of `binnacle-core`'s surface it may depend on. Not
  deciding it now, while `binnacle-router` is empty, is a legitimate
  deferral; not deciding it *ever* would not be.

## 10. Migration outline (implementation-plan level, listed for completeness)

1. Move `src/binnacle/` → `packages/binnacle-core/src/binnacle_core/`; update
   all internal imports.
2. Move `tests/` → `packages/binnacle-core/tests/`.
3. Move `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, `docs/components/*` →
   `docs/binnacle-core/`.
4. Write `docs/OVERVIEW.md` (new).
5. Restructure root `pyproject.toml` into a `uv` workspace manifest; create
   `packages/binnacle-core/pyproject.toml` with the moved `[project]`,
   `[tool.mypy]`, `[tool.importlinter]` (root_package updated) settings.
6. Scaffold `packages/binnacle-router/` (empty Python package) and
   `packages/binnacle-ui/` (empty TypeScript/React package), each with a
   skeleton `docs/<package>/{REQUIREMENTS,ARCHITECTURE}.md`.
7. Add root `pnpm-workspace.yaml`.
8. Update `.pre-commit-config.yaml` (new hooks, `pre-push` stage) and
   `.github/workflows/ci.yml` (loop over packages, add JS steps).
9. Create `docs/adr/0001-monorepo-restructure.md` recording this change,
   including the DR-6 breaking-change call-out and the SemVer bump it forces.
10. Update `GUIDELINES.md`'s path references per §5.2 above (**not** the
    ADR-location text — that's a deferred follow-up, see §5.2).
11. Create per-package `CHANGELOG.md` files. **Fold the existing root
    `CHANGELOG.md`'s history into `packages/binnacle-core/CHANGELOG.md`** (not
    left as an "or" — every existing entry describes what is now
    `binnacle-core`), then remove the root file.
12. Rewrite the root `README.md` as a monorepo landing page (what the three
    packages are, links to each); move today's installation/usage/API-detail
    content into `packages/binnacle-core/README.md`, since that content is
    specific to installing and using `binnacle-core`, not the monorepo as a
    whole.
13. Update `.gitignore` for the JS toolchain entering the repo for the first
    time (`node_modules/`, build output, etc.).
14. Document (and where possible, script — e.g. a `scripts/dev-setup.sh`) that
    contributor setup runs `pre-commit install --hook-type pre-commit
    --hook-type pre-push`, not just `pre-commit install` — see the §8 gap.

A full step-by-step implementation plan (with verification per step) is the
next artifact, produced by the `writing-plans` skill once this spec is
approved.

## 11. Open questions

- **§7's TypeScript choice** — flagged for explicit review since it was a
  default I applied, not something directly asked.
- **The exact SemVer bump for the rename (DR-6)** — deliberately not
  pre-decided here; GUIDELINES §11 requires proposing it and getting
  explicit confirmation at implementation time, not baking it into a design
  spec written before the change exists.
- **Whether `scripts/check.sh` should self-provision an ephemeral Postgres**
  (§8) — a convenience call for the implementation plan, not this spec.
- **Writing the ADR location/format into GUIDELINES §5.2's own text** — the
  location is decided and used (§5.1, §10 step 9), but updating GUIDELINES
  itself to document it is explicitly deferred to a follow-up after this
  restructuring lands, not part of this migration.
