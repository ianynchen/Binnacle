# Monorepo Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the single-package `binnacle` repo into a `uv`/`pnpm` monorepo hosting `packages/binnacle-core` (the existing library, renamed), `packages/binnacle-router` (empty scaffold), and `packages/binnacle-ui` (empty scaffold), with per-package docs/changelogs and a pre-commit/pre-push/CI quality-gate split.

**Architecture:** Move `src/binnacle` → `packages/binnacle-core/src/binnacle_core` verbatim (logic unchanged, import path renamed), scaffold the two new empty packages next to it, split root `pyproject.toml` into a `uv` workspace manifest plus one manifest per Python package, add a `pnpm` workspace for the JS package, and rewire `scripts/check.sh`, `.github/workflows/ci.yml`, and `.pre-commit-config.yaml` to operate across all three.

**Tech Stack:** Python ≥3.13 / `uv` workspaces (existing), TypeScript + React + Biome + Vitest (new, for `binnacle-ui`), `pnpm` workspaces (new).

**Spec:** [docs/superpowers/specs/2026-09-04-monorepo-restructure-design.md](../specs/2026-09-04-monorepo-restructure-design.md)

## Global Constraints

- Naming: package directory, distribution name, and docs directory are always identical, hyphenated (`binnacle-core`, `binnacle-router`, `binnacle-ui`); Python's unavoidable underscore equivalent (`binnacle_core`, `binnacle_router`) is the only divergence (spec §4).
- No behavior change to `binnacle-core`'s logic — this plan moves and renames, it does not touch application/domain logic (spec §2).
- `binnacle-service` is out of scope entirely (spec §2).
- `docs/adr/` location is used now for this change's own ADR, but writing that convention into GUIDELINES §5.2's text is explicitly deferred to a follow-up, not part of this plan (spec §5.1/§5.2, per explicit user direction).
- The rename (`binnacle` → `binnacle-core`) is a breaking change to the public import surface (`import binnacle` stops working) — GUIDELINES §11 requires proposing the exact SemVer bump and getting explicit confirmation before applying it. Do not pick a number unilaterally (Task 11).
- `[tool.ruff]` stays shared at the root `pyproject.toml` only — do not add a `[tool.ruff]` section to any package's own `pyproject.toml` (it would stop Ruff's upward config search and silently orphan that package from the shared style; spec §6, verified against Ruff's config-discovery docs).
- `uv` workspace `members` is an explicit path list, never a `packages/*` glob (spec §6, verified against `uv`'s workspace docs — a glob hard-errors on `binnacle-ui`, which has no `pyproject.toml`).

---

### Task 1: Move `binnacle-core` into the workspace and rename its import

**Files:**
- Move: `src/binnacle/` → `packages/binnacle-core/src/binnacle_core/`
- Move: `tests/` → `packages/binnacle-core/tests/`
- Create: `packages/binnacle-core/pyproject.toml`
- Modify: `pyproject.toml` (repo root — becomes the `uv` workspace manifest)
- Modify (import rename, in place after the move): every `.py` file under `packages/binnacle-core/`

**Interfaces:**
- Produces: the `binnacle_core` package (importable from anywhere in the workspace once `uv sync` runs at the root), and the `uv` workspace root that Tasks 2 and 5 extend.

- [ ] **Step 1: Confirm the working tree is clean before a large move**

Run: `git status --porcelain`
Expected: empty output. If not empty, stop and ask before proceeding — this task moves hundreds of files and an unrelated dirty change would be impossible to disentangle afterward.

- [ ] **Step 2: Move the source tree**

```bash
mkdir -p packages/binnacle-core/src
git mv src/binnacle packages/binnacle-core/src/binnacle_core
git mv tests packages/binnacle-core/tests
rmdir src 2>/dev/null || true
```

- [ ] **Step 3: Rename the internal import path**

Only lines that are actual `import`/`from` statements referencing the module path are touched — not the `Binnacle`/`BinnacleConfig`/`BinnacleError` class names, not the `schema_name: str = "binnacle"` default, not the `Actor("engine", "binnacle")` literal, none of which are import statements:

```bash
find packages/binnacle-core -name '*.py' -print0 | xargs -0 sed -i '' \
  -e 's/^from binnacle\./from binnacle_core./' \
  -e 's/^from binnacle import/from binnacle_core import/' \
  -e 's/^import binnacle\./import binnacle_core./' \
  -e 's/^import binnacle$/import binnacle_core/'
```

(On Linux, drop the empty string after `-i`: `sed -i -e ...`.)

- [ ] **Step 4: Verify the rename is complete and nothing was missed**

Run: `grep -rn '^from binnacle import\|^from binnacle\.\|^import binnacle\.\|^import binnacle$' packages/binnacle-core --include='*.py'`
Expected: no output. If anything prints, Step 3's sed missed a form (e.g. `import binnacle as x`) — fix that line by hand and re-run this check.

- [ ] **Step 5: Write `packages/binnacle-core/pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "binnacle-core"
version = "0.2.0"
description = "PostgreSQL-backed decision-record library."
requires-python = ">=3.13"
dependencies = [
  "pydantic>=2.9",
  "psycopg[binary,pool]==3.3.5",
  "pgvector==0.5.0",
  "yoyo-migrations==9.0.0",
]

[tool.hatch.build.targets.wheel]
packages = ["src/binnacle_core"]

[tool.mypy]
python_version = "3.13"
strict = true

[[tool.mypy.overrides]]
# yoyo-migrations ships no py.typed marker / stubs (house precedent: narrowly
# scoped override for a single untyped third-party dependency, not a blanket
# relaxation of strict mode).
module = "yoyo.*"
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
  # Registered (not deselected): seeding 10k decisions/100k transitions and
  # running it measured ~14s locally, and the full suite ~34s total -- well
  # under the "mark + addopts-deselect if >2min" threshold the task set, so
  # it stays in the default `pytest`/check.sh run rather than needing a
  # separate CI invocation.
  "perf: NFR-7 seeded design-scale (10k decisions/100k transitions) timing assertions (tests/db/test_perf.py).",
]

[tool.importlinter]
root_package = "binnacle_core"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "layers"
type = "layers"
layers = ["binnacle_core.adapters", "binnacle_core.application", "binnacle_core.domain"]

[[tool.importlinter.contracts]]
name = "domain is pure"
type = "forbidden"
source_modules = ["binnacle_core.domain"]
forbidden_modules = ["psycopg", "yoyo", "pgvector"]

[[tool.importlinter.contracts]]
name = "application is driver-free"
type = "forbidden"
source_modules = ["binnacle_core.application"]
forbidden_modules = ["psycopg", "yoyo"]
```

Note: deliberately no `[tool.ruff]` section here — see Global Constraints.

- [ ] **Step 6: Rewrite the root `pyproject.toml` as the `uv` workspace manifest**

```toml
[project]
name = "binnacle-workspace"
version = "0.0.0"
requires-python = ">=3.13"
dependencies = []

[tool.uv]
package = false

[tool.uv.workspace]
members = ["packages/binnacle-core"]

[dependency-groups]
dev = [
  "pytest",
  "pytest-asyncio",
  "mypy",
  "ruff",
  "import-linter",
  "pre-commit",
]

[tool.ruff]
target-version = "py313"
line-length = 100
```

- [ ] **Step 7: Re-sync the workspace environment**

Run: `uv sync`
Expected: exits 0, resolves and installs `binnacle-core` plus the shared dev dependency group into the workspace's single virtual environment.

- [ ] **Step 8: Verify formatting and lint pass unchanged**

Run: `uv run ruff format --check packages/binnacle-core/src packages/binnacle-core/tests && uv run ruff check packages/binnacle-core/src packages/binnacle-core/tests`
Expected: both exit 0 with no diffs/violations (the moved files are byte-identical apart from the Step 3 import rename, which is already `ruff format`-clean since it only changed module paths on existing lines).

- [ ] **Step 9: Verify mypy strict still passes**

Run: `uv run mypy --config-file packages/binnacle-core/pyproject.toml packages/binnacle-core/src packages/binnacle-core/tests/unit/test_typing_narrowing.py`
Expected: `Success: no issues found`.

- [ ] **Step 10: Verify the import-linter layering contract still holds**

Run: `uv run lint-imports --config packages/binnacle-core/pyproject.toml`
Expected: all three contracts (`layers`, `domain is pure`, `application is driver-free`) report kept.

- [ ] **Step 11: Verify the full test suite still passes from its new location**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests -q`
Expected: same pass count as before the move (integration tests under `tests/db` skip cleanly if `BINNACLE_TEST_DSN` is unreachable, same as pre-move behavior — this is not a regression).

- [ ] **Step 12: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: move binnacle-core into packages/, rename import to binnacle_core

Structural move only -- no logic change. src/binnacle -> packages/
binnacle-core/src/binnacle_core, tests/ -> packages/binnacle-core/tests/.
Root pyproject.toml becomes a uv workspace manifest; binnacle-core gets
its own pyproject.toml with the moved project/mypy/import-linter config
(root_package updated to binnacle_core). This is a breaking rename to
the public import surface -- see docs/superpowers/specs/
2026-09-04-monorepo-restructure-design.md DR-6; the SemVer bump is
proposed separately in a later task, not applied here.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Scaffold `packages/binnacle-router` (empty Python package)

**Files:**
- Create: `packages/binnacle-router/pyproject.toml`
- Create: `packages/binnacle-router/src/binnacle_router/__init__.py`
- Create: `packages/binnacle-router/tests/__init__.py`
- Create: `packages/binnacle-router/tests/test_package.py`
- Create: `packages/binnacle-router/CHANGELOG.md`
- Modify: root `pyproject.toml` (`[tool.uv.workspace] members`)

**Interfaces:**
- Consumes: the workspace root from Task 1.
- Produces: an importable, empty `binnacle_router` package other tasks (5, 6, 9) can wire CI/pre-commit/docs around.

- [ ] **Step 1: Write `packages/binnacle-router/pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "binnacle-router"
version = "0.1.0"
description = "REST + MCP surface for binnacle-core (library, not a service)."
requires-python = ">=3.13"
dependencies = []

[tool.hatch.build.targets.wheel]
packages = ["src/binnacle_router"]

[tool.mypy]
python_version = "3.13"
strict = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.importlinter]
root_package = "binnacle_router"
include_external_packages = true
```

Note: no contracts registered under `[tool.importlinter]` yet — there is only one file to layer. The first contract (and the cross-package rule for how `binnacle-router` may depend on `binnacle-core`, deferred per spec DR-7) is `binnacle-router`'s own future spec's responsibility, not this scaffold's.

- [ ] **Step 2: Write the placeholder module**

```python
"""binnacle-router: REST + MCP surface for binnacle-core.

Scaffold only -- no functionality yet. See its own future spec/plan
(deferred from docs/superpowers/specs/2026-09-04-monorepo-restructure-design.md
§1) for the actual REST/MCP surface design.
"""

__version__ = "0.1.0"
```

Save as `packages/binnacle-router/src/binnacle_router/__init__.py`.

- [ ] **Step 3: Write a real smoke test (not a placeholder)**

```python
import binnacle_router


def test_package_is_importable_from_the_workspace_env() -> None:
    """Confirms the workspace/uv wiring actually installs this package,
    not just that Python syntax is valid -- the failure mode this guards
    against is a workspace members list or pyproject.toml typo that
    silently leaves the package unbuilt."""
    assert binnacle_router.__version__ == "0.1.0"
```

Save as `packages/binnacle-router/tests/test_package.py`. Create empty `packages/binnacle-router/tests/__init__.py` alongside it.

- [ ] **Step 4: Write `packages/binnacle-router/CHANGELOG.md`**

```markdown
# Changelog

All notable changes to `binnacle-router` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/) and
[SemVer](https://semver.org/).

## [Unreleased]

### Added

- Initial package scaffold (no functionality yet).
```

- [ ] **Step 5: Add the package to the workspace**

Edit `pyproject.toml` at the repo root:

```toml
[tool.uv.workspace]
members = ["packages/binnacle-core", "packages/binnacle-router"]
```

- [ ] **Step 6: Re-sync and verify**

Run: `uv sync`
Expected: exits 0, `binnacle-router` now resolves in the workspace environment.

Run: `uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests -q`
Expected: 1 passed.

Run: `uv run mypy --config-file packages/binnacle-router/pyproject.toml packages/binnacle-router/src`
Expected: `Success: no issues found`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat: scaffold binnacle-router as an empty workspace package

No functionality yet -- establishes the package, its pyproject.toml,
and a smoke test proving the uv workspace actually builds it. Its
REST/MCP surface is designed in a future spec (deferred from
docs/superpowers/specs/2026-09-04-monorepo-restructure-design.md §1).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Scaffold `packages/binnacle-ui` (empty TypeScript/React package)

**Files:**
- Create: `packages/binnacle-ui/package.json`
- Create: `packages/binnacle-ui/tsconfig.json`
- Create: `packages/binnacle-ui/biome.json`
- Create: `packages/binnacle-ui/vitest.config.ts`
- Create: `packages/binnacle-ui/src/Placeholder.tsx`
- Create: `packages/binnacle-ui/src/Placeholder.test.tsx`
- Create: `packages/binnacle-ui/src/index.ts`
- Create: `packages/binnacle-ui/CHANGELOG.md`
- Create: `pnpm-workspace.yaml` (repo root)

**Interfaces:**
- Produces: an importable `binnacle-ui` package exporting `Placeholder`, proving the whole TypeScript+React+Biome+Vitest toolchain actually compiles, lints, and tests end to end — later tasks (5, 6, 9) wire CI/pre-commit/docs around it.

- [ ] **Step 1: Install `pnpm`**

`pnpm` isn't present on this machine and Node's bundled `corepack` isn't either (checked: neither `pnpm` nor `corepack` resolve). Install it directly via `npm`, which is present:

Run: `npm install -g pnpm`
Expected: exits 0. Verify with `pnpm --version`.

- [ ] **Step 2: Write the root `pnpm-workspace.yaml`**

```yaml
packages:
  - "packages/binnacle-ui"
```

- [ ] **Step 3: Write `packages/binnacle-ui/package.json`**

```json
{
  "name": "binnacle-ui",
  "version": "0.1.0",
  "description": "UI components for binnacle (review queue, decision history, precedent search, etc.)",
  "type": "module",
  "private": false,
  "main": "./dist/index.js",
  "types": "./dist/index.d.ts",
  "scripts": {
    "lint": "biome check .",
    "format": "biome format --write .",
    "typecheck": "tsc --noEmit",
    "test": "vitest run",
    "build": "tsc -p tsconfig.json --emitDeclarationOnly && vite build"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@biomejs/biome": "^1.9.4",
    "@testing-library/react": "^16.0.1",
    "@types/react": "^18.3.12",
    "@types/react-dom": "^18.3.1",
    "@vitejs/plugin-react": "^4.3.4",
    "jsdom": "^25.0.1",
    "typescript": "^5.7.2",
    "vite": "^6.0.3",
    "vitest": "^2.1.8"
  }
}
```

Note: `vite build` is used here as the simplest way to get a working `build` script wired for verification in Step 9 — picking the final shipping bundler (`tsup` vs. Vite library mode) is explicitly left to `binnacle-ui`'s own future spec per the design spec §7; nothing later depends on this choice being final.

- [ ] **Step 4: Write `packages/binnacle-ui/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM"],
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "jsx": "react-jsx",
    "strict": true,
    "declaration": true,
    "declarationDir": "./dist",
    "outDir": "./dist",
    "esModuleInterop": true,
    "skipLibCheck": true,
    "isolatedModules": true
  },
  "include": ["src"]
}
```

- [ ] **Step 5: Write `packages/binnacle-ui/biome.json`**

```json
{
  "$schema": "https://biomejs.dev/schemas/1.9.4/schema.json",
  "organizeImports": { "enabled": true },
  "linter": {
    "enabled": true,
    "rules": { "recommended": true }
  },
  "formatter": {
    "enabled": true,
    "indentStyle": "space",
    "indentWidth": 2
  }
}
```

- [ ] **Step 6: Write `packages/binnacle-ui/vitest.config.ts`**

```typescript
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
  },
});
```

- [ ] **Step 7: Write the placeholder component and its export**

```tsx
export function Placeholder(): JSX.Element {
  return <div data-testid="binnacle-ui-placeholder">binnacle-ui scaffold</div>;
}
```

Save as `packages/binnacle-ui/src/Placeholder.tsx`.

```typescript
export { Placeholder } from "./Placeholder";
```

Save as `packages/binnacle-ui/src/index.ts`.

- [ ] **Step 8: Write a real smoke test (not a placeholder)**

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Placeholder } from "./Placeholder";

describe("Placeholder", () => {
  it("renders, proving the React + TypeScript + Vitest toolchain is wired correctly", () => {
    render(<Placeholder />);
    expect(screen.getByTestId("binnacle-ui-placeholder")).toBeInTheDocument();
  });
});
```

Save as `packages/binnacle-ui/src/Placeholder.test.tsx`.

- [ ] **Step 9: Write `packages/binnacle-ui/CHANGELOG.md`**

```markdown
# Changelog

All notable changes to `binnacle-ui` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/) and
[SemVer](https://semver.org/).

## [Unreleased]

### Added

- Initial package scaffold (React + TypeScript + Biome + Vitest wired,
  one placeholder component proving the toolchain works end to end).
```

- [ ] **Step 10: Install dependencies and verify the whole toolchain**

Run: `pnpm install` (from the repo root — resolves via `pnpm-workspace.yaml`)
Expected: exits 0.

Run: `pnpm --filter binnacle-ui lint`
Expected: exits 0, no lint errors.

Run: `pnpm --filter binnacle-ui typecheck`
Expected: exits 0.

Run: `pnpm --filter binnacle-ui test`
Expected: 1 passed.

Run: `pnpm --filter binnacle-ui build`
Expected: exits 0, produces `packages/binnacle-ui/dist/index.js` and `dist/index.d.ts`.

- [ ] **Step 11: Ignore build/dependency artifacts**

Confirm `node_modules/` and `dist/` are not staged in the next step (Task 7 updates `.gitignore` properly — for now, stage explicitly rather than `git add -A` to avoid committing them by accident):

```bash
git add packages/binnacle-ui pnpm-workspace.yaml
git status --porcelain packages/binnacle-ui | grep -E 'node_modules|dist/' && echo "STOP: build artifacts staged, do not commit" || echo "clean"
```

Expected: `clean`. If artifacts are staged, unstage them (`git restore --staged <path>`) before continuing — `.gitignore` isn't updated until Task 7, so this step guards against committing `node_modules/` in the meantime.

- [ ] **Step 12: Commit**

```bash
git commit -m "$(cat <<'EOF'
feat: scaffold binnacle-ui as an empty workspace package

TypeScript + React + Biome + Vitest, one placeholder component with a
render test proving the toolchain compiles, lints, and tests end to
end. Final bundler choice for shipping (tsup vs. Vite library mode) is
left to binnacle-ui's own future spec -- this scaffold's `build` script
is a working default, not a final decision.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Restructure docs — per-package REQUIREMENTS/ARCHITECTURE, new OVERVIEW.md

**Files:**
- Move: `docs/REQUIREMENTS.md` → `docs/binnacle-core/REQUIREMENTS.md`
- Move: `docs/ARCHITECTURE.md` → `docs/binnacle-core/ARCHITECTURE.md`
- Move: `docs/components/` → `docs/binnacle-core/components/`
- Create: `docs/OVERVIEW.md`
- Create: `docs/binnacle-router/REQUIREMENTS.md`
- Create: `docs/binnacle-router/ARCHITECTURE.md`
- Create: `docs/binnacle-ui/REQUIREMENTS.md`
- Create: `docs/binnacle-ui/ARCHITECTURE.md`
- Modify: `docs/binnacle-core/ARCHITECTURE.md` (package-layout section, now-stale paths)

**Interfaces:**
- Consumes: Tasks 1–3 (needs the final package names/paths to describe accurately).
- Produces: the doc tree Task 9 (GUIDELINES) points at.

- [ ] **Step 1: Move the core docs**

```bash
mkdir -p docs/binnacle-core
git mv docs/REQUIREMENTS.md docs/binnacle-core/REQUIREMENTS.md
git mv docs/ARCHITECTURE.md docs/binnacle-core/ARCHITECTURE.md
git mv docs/components docs/binnacle-core/components
```

- [ ] **Step 2: Fix the now-stale package-layout section in `docs/binnacle-core/ARCHITECTURE.md`**

Find the `## 6. Package Layout and Technology` section's code block (currently starts `src/binnacle/`) and replace it with:

```
packages/binnacle-core/src/binnacle_core/
  domain/        models.py (Decision, Ref, Link, Transition, Actor, enums,
                 CandidatePair, Suggestion, projections)  errors.py
  application/   client.py recorder.py lifecycle.py queue.py query.py
                 discovery.py archival.py export.py ports.py config.py
  adapters/      postgres_store.py
```

Also fix the inline reference near the top of the file (`Architecture for **Binnacle** (contract: \`docs/REQUIREMENTS.md\`)`) to `` `REQUIREMENTS.md` `` (same-directory relative reference now that both files live in `docs/binnacle-core/`).

- [ ] **Step 3: Write `docs/OVERVIEW.md`**

```markdown
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
```

- [ ] **Step 4: Write `docs/binnacle-router/REQUIREMENTS.md`**

```markdown
# REQUIREMENTS — binnacle-router

Status: scaffold. `binnacle-router` is an empty package (see
`docs/superpowers/plans/2026-09-04-monorepo-restructure.md` Task 2) — it
has no functional requirements yet.

Its role (from `docs/OVERVIEW.md`): a library, not a service, exposing
`binnacle-core`'s functionality as REST + MCP, so a host process — a
standalone binnacle deployment, or another service such as Meridian — can
mount it.

Functional and non-functional requirements are defined in this package's
own future design spec, produced through the same
brainstorming-spec-plan cycle as this restructure. Until that spec exists,
no endpoint, tool, or contract described here should be treated as
committed.
```

- [ ] **Step 5: Write `docs/binnacle-router/ARCHITECTURE.md`**

```markdown
# ARCHITECTURE — binnacle-router

Status: scaffold. See `docs/binnacle-router/REQUIREMENTS.md` — this
package has no functional design yet.

Known constraints from the monorepo restructure that any future design
must satisfy:

- Python ≥3.13, distribution name `binnacle-router`, import name
  `binnacle_router` (`docs/OVERVIEW.md` §2).
- Must declare, and enforce via an import-linter contract, exactly which
  part of `binnacle-core`'s surface it depends on — this was explicitly
  deferred by the restructure (`docs/OVERVIEW.md` §4) and is this
  package's own first architectural decision to make.
- Layering, dependency direction, and any internal module structure are
  documented and enforced here once real code exists (GUIDELINES §8).
```

- [ ] **Step 6: Write `docs/binnacle-ui/REQUIREMENTS.md`**

```markdown
# REQUIREMENTS — binnacle-ui

Status: scaffold. `binnacle-ui` is an empty package (one placeholder
component; see `docs/superpowers/plans/2026-09-04-monorepo-restructure.md`
Task 3) — it has no functional requirements yet.

Its role (from `docs/OVERVIEW.md`): JS/TypeScript UI components (review
queue, decision history, precedent search, etc.) usable by any consumer
that embeds binnacle.

The actual component set is defined in this package's own future design
spec. Until that spec exists, no component described here should be
treated as committed.
```

- [ ] **Step 7: Write `docs/binnacle-ui/ARCHITECTURE.md`**

```markdown
# ARCHITECTURE — binnacle-ui

Status: scaffold. See `docs/binnacle-ui/REQUIREMENTS.md` — this package
has no functional design yet.

Decided by the monorepo restructure (`docs/superpowers/specs/
2026-09-04-monorepo-restructure-design.md` §7), binding on any future
design:

- TypeScript, not plain JS.
- React.
- Biome for lint + format (not ESLint + Prettier).
- Vitest for unit tests.
- Bundler for shipping the package (`tsup` vs. Vite library mode) is
  explicitly **not** decided yet — the scaffold's `build` script uses Vite
  as a working default, not a final choice (spec §7, plan Task 3 Step 3).
```

- [ ] **Step 8: Verify no broken relative links were left behind**

Run: `grep -rn 'docs/REQUIREMENTS.md\|docs/ARCHITECTURE.md\|docs/components/' --include='*.md' . | grep -v docs/superpowers`
Expected: no output (README.md's references are fixed in Task 7, GUIDELINES.md's in Task 9 — if this grep shows hits in either of those two files at this point, that's expected and will be resolved by those later tasks, not a failure of this one; hits anywhere else would be a real miss).

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
docs: restructure docs per package, add OVERVIEW.md

docs/REQUIREMENTS.md, docs/ARCHITECTURE.md, docs/components/ move to
docs/binnacle-core/ (moved as-is apart from the now-stale package-layout
code block, updated to the new paths). docs/OVERVIEW.md is new: the
system-level context that used to live in ARCHITECTURE.md's C4 L1
section. binnacle-router and binnacle-ui get skeleton REQUIREMENTS/
ARCHITECTURE docs stating their role and deferring functional design to
their own future specs.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Rewrite `scripts/check.sh` and `.github/workflows/ci.yml` for the workspace

**Files:**
- Modify: `scripts/check.sh`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: Tasks 1–3 (needs all three packages to exist to check them).
- Produces: `scripts/check.sh`, which Task 6's `pre-push` hook invokes directly.

- [ ] **Step 1: Rewrite `scripts/check.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "== binnacle-core =="
uv run ruff format --check packages/binnacle-core/src packages/binnacle-core/tests
uv run ruff check packages/binnacle-core/src packages/binnacle-core/tests
# `mypy src` is the strict-mode contract; `tests/` isn't otherwise type-checked.
# `tests/unit/test_typing_narrowing.py` is the one deliberate, narrow exception:
# it exists solely to guard relevant()'s @overload narrowing (Binnacle/StorePort/
# PostgresStore) with `typing.assert_type`, which only fails anything under mypy
# (a runtime no-op otherwise) -- so it must actually run under mypy to guard
# anything. See that file's module docstring.
uv run mypy --config-file packages/binnacle-core/pyproject.toml \
  packages/binnacle-core/src packages/binnacle-core/tests/unit/test_typing_narrowing.py
uv run lint-imports --config packages/binnacle-core/pyproject.toml
uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests -q

echo "== binnacle-router =="
uv run ruff format --check packages/binnacle-router/src packages/binnacle-router/tests
uv run ruff check packages/binnacle-router/src packages/binnacle-router/tests
uv run mypy --config-file packages/binnacle-router/pyproject.toml packages/binnacle-router/src
uv run lint-imports --config packages/binnacle-router/pyproject.toml
uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests -q

echo "== binnacle-ui =="
pnpm --filter binnacle-ui lint
pnpm --filter binnacle-ui typecheck
pnpm --filter binnacle-ui test
pnpm --filter binnacle-ui build
```

- [ ] **Step 2: Run it locally to verify**

Run: `bash scripts/check.sh`
Expected: all three sections pass, exits 0.

- [ ] **Step 3: Rewrite `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  check:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg18
        env:
          POSTGRES_DB: binnacle_test
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgres
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    env:
      BINNACLE_TEST_DSN: postgresql://postgres:postgres@localhost:5432/binnacle_test
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v6
        with:
          python-version: "3.13"

      - name: Install pnpm
        uses: pnpm/action-setup@v4
        with:
          version: 9

      - name: Set up Node
        uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: "pnpm"

      - name: Enable pgvector extension
        run: |
          sudo apt-get update && sudo apt-get install -y postgresql-client
          PGPASSWORD=postgres psql -h localhost -U postgres -d binnacle_test -c "CREATE EXTENSION IF NOT EXISTS vector"

      - name: Install Python dependencies
        run: uv sync

      - name: Install JS dependencies
        run: pnpm install

      - name: Run gates
        run: bash scripts/check.sh

      - name: Run pre-commit
        run: uvx pre-commit run --all-files --hook-stage push
```

- [ ] **Step 4: Commit**

```bash
git add scripts/check.sh .github/workflows/ci.yml
git commit -m "$(cat <<'EOF'
ci: run gates across all three workspace packages

scripts/check.sh now loops binnacle-core and binnacle-router through
ruff/mypy/import-linter/pytest, and runs binnacle-ui's lint/typecheck/
test/build. CI installs pnpm and Node alongside the existing uv setup,
and runs the pre-commit push-stage hooks (not just the default stage)
so a broken .pre-commit-config.yaml fails CI too.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Update pre-commit hooks — add the `pre-push` stage and `binnacle-ui` linting

**Files:**
- Modify: `.pre-commit-config.yaml`
- Create: `scripts/dev-setup.sh`

**Interfaces:**
- Consumes: `scripts/check.sh` from Task 5 (the `pre-push` hook invokes it directly).

- [ ] **Step 1: Rewrite `.pre-commit-config.yaml`**

```yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.30.1
    hooks: [{id: gitleaks}]
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.5
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: local
    hooks:
      - id: biome-check
        name: biome check (binnacle-ui)
        entry: pnpm --filter binnacle-ui exec biome check --write
        language: system
        files: ^packages/binnacle-ui/
        types_or: [ts, tsx, json]
      - id: full-check
        name: full check (mypy, import-linter, all tests, ui build) -- same script CI runs
        entry: bash scripts/check.sh
        language: system
        pass_filenames: false
        stages: [pre-push]
```

- [ ] **Step 2: Write `scripts/dev-setup.sh`**

`pre-commit install` alone only wires the default (`pre-commit`) hook stage — it does **not** install `pre-push` hooks. Left undocumented, the `full-check` hook above would silently never run for anyone who only ran the "normal" setup command, and the whole "what's pushed is what CI checks" guarantee (spec DR-5) becomes false without anyone noticing. Script both installs so this can't be missed:

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "Installing Python dependencies..."
uv sync

echo "Installing JS dependencies..."
pnpm install

echo "Installing git hooks (pre-commit AND pre-push stages)..."
uv run pre-commit install --hook-type pre-commit --hook-type pre-push

echo "Done. 'git commit' runs fast checks (gitleaks, ruff, biome);"
echo "'git push' runs the full gate (mypy, import-linter, all tests, ui build)."
```

- [ ] **Step 3: Make it executable and verify**

```bash
chmod +x scripts/dev-setup.sh
bash scripts/dev-setup.sh
```

Expected: exits 0, ends with the two echoed lines above.

Run: `git config --get-all core.hooksPath || cat .git/hooks/pre-push | head -1`
Expected: the `pre-push` hook file exists and is pre-commit-framework-generated (starts with a pre-commit shebang/marker), confirming Step 2's install actually wired the push stage — not just the default one.

- [ ] **Step 4: Verify the pre-commit stage still runs fast checks only**

Run: `uv run pre-commit run --all-files`
Expected: runs gitleaks, ruff, ruff-format, and biome-check only — NOT `full-check` (its `stages: [pre-push]` excludes it from the default `--all-files` invocation, which only runs default-stage hooks).

Run: `uv run pre-commit run --all-files --hook-stage push`
Expected: additionally runs `full-check`, which internally calls `scripts/check.sh` (Task 5) — same exit behavior as running that script directly.

- [ ] **Step 5: Commit**

```bash
git add .pre-commit-config.yaml scripts/dev-setup.sh
git commit -m "$(cat <<'EOF'
ci: split pre-commit into a fast default stage and a full pre-push stage

Default stage (every commit): gitleaks, ruff, ruff-format, biome for
binnacle-ui -- all fast, no I/O. New pre-push stage: a single hook
invoking scripts/check.sh directly, the same script CI runs, so "it
passed my push" and "it'll pass CI" are the same statement (spec DR-5).
scripts/dev-setup.sh scripts the two-stage hook install explicitly --
`pre-commit install` alone only wires the default stage, and leaving
that as tribal knowledge would silently defeat the whole guarantee.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Rewrite the root README, add `binnacle-core`'s own README, update `.gitignore`

**Files:**
- Modify: `README.md` (repo root — becomes a monorepo landing page)
- Create: `packages/binnacle-core/README.md` (today's installation/usage/API content, moved and updated)
- Modify: `.gitignore` (add JS toolchain artifacts)

**Interfaces:**
- Consumes: Tasks 1–4 (needs final package names and doc paths).

- [ ] **Step 1: Move today's README content to `packages/binnacle-core/README.md`, updating for the rename**

```bash
git mv README.md packages/binnacle-core/README.md
```

Then, within `packages/binnacle-core/README.md`, apply these exact fixes (everything else — the prose, the API walkthrough, the code samples' logic — is unchanged, since none of it describes behavior that changed):

1. The icon `<picture>` line at the top: the `src=`/`srcset=` paths (`docs/assets/icon-dark.svg`, `docs/assets/icon-light.svg`) become `../../docs/assets/icon-light.svg` and `../../docs/assets/icon-dark.svg` (relative from the new location) — or, simpler and more robust, drop the icon block from this file entirely (it now belongs on the root README as the monorepo's identity, added in Step 2) and keep just the `# Binnacle` heading here.
2. Every `[`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md)` / `[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)` link → `[`../../docs/binnacle-core/REQUIREMENTS.md`](../../docs/binnacle-core/REQUIREMENTS.md)` / `[`../../docs/binnacle-core/ARCHITECTURE.md`](../../docs/binnacle-core/ARCHITECTURE.md)` (two occurrences: the intro paragraph, and the "Limitations" section's closing reference).
3. `## Install` code block: `uv add "binnacle @ git+..."` / `pip install "binnacle @ git+..."` → `uv add "binnacle-core @ git+https://github.com/ianynchen/Binnacle.git#subdirectory=packages/binnacle-core"` / `pip install "binnacle-core @ git+https://github.com/ianynchen/Binnacle.git#subdirectory=packages/binnacle-core"` (a `git+` install of one package inside a monorepo needs the `#subdirectory=` fragment; both `uv` and `pip` support it).
4. Every `from binnacle import ...` code sample (Quickstart, Configuration, Recording, Error handling — 4 occurrences) → `from binnacle_core import ...`.
5. "### The guardrail stack" section: `binnacle.adapters → binnacle.application → binnacle.domain` → `binnacle_core.adapters → binnacle_core.application → binnacle_core.domain`.
6. "## Limitations" section's two file references: `` `src/binnacle/application/query.py` `` → `` `packages/binnacle-core/src/binnacle_core/application/query.py` ``, and the same for `discovery.py`.

Run this to confirm nothing was missed:

Run: `grep -n 'from binnacle import\|docs/REQUIREMENTS.md\|docs/ARCHITECTURE.md\|src/binnacle/' packages/binnacle-core/README.md`
Expected: no output.

- [ ] **Step 2: Write the new root `README.md`**

```markdown
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/icon-dark.svg">
  <img alt="Binnacle icon" src="docs/assets/icon-light.svg" width="64" height="136">
</picture>

# Binnacle

A monorepo for Binnacle: the fleet's decision record and precedent engine.
See [`docs/OVERVIEW.md`](docs/OVERVIEW.md) for how the packages below relate,
and each package's own README for installation and usage.

## Packages

| Package | What it is |
|---|---|
| [`packages/binnacle-core`](packages/binnacle-core/README.md) | The decision-record library: domain model, lifecycle engine, PostgreSQL/pgvector store. |
| [`packages/binnacle-router`](packages/binnacle-router) | A library (not a service) exposing `binnacle-core` as REST + MCP. Scaffold only — see [`docs/binnacle-router/REQUIREMENTS.md`](docs/binnacle-router/REQUIREMENTS.md). |
| [`packages/binnacle-ui`](packages/binnacle-ui) | JS/TypeScript UI components for consumers that embed binnacle. Scaffold only — see [`docs/binnacle-ui/REQUIREMENTS.md`](docs/binnacle-ui/REQUIREMENTS.md). |

## Development

This is a `uv` workspace (Python: `binnacle-core`, `binnacle-router`) plus a
`pnpm` workspace (JS: `binnacle-ui`). One-time setup:

```bash
bash scripts/dev-setup.sh
```

Run every gate across all three packages — the same script CI runs:

```bash
bash scripts/check.sh
```

See [`GUIDELINES.md`](GUIDELINES.md) for house standards and process, and
[`docs/OVERVIEW.md`](docs/OVERVIEW.md) for the monorepo's structure and
tooling decisions in full.
```

- [ ] **Step 3: Update `.gitignore` for the JS toolchain**

Append to the existing `.gitignore`:

```
# JS (binnacle-ui)
node_modules/
packages/binnacle-ui/dist/
*.tsbuildinfo
```

- [ ] **Step 4: Verify no tracked files match the new ignore patterns**

Run: `git status --porcelain packages/binnacle-ui | grep -E 'node_modules|dist/'`
Expected: no output (Task 3 Step 11 already guarded against this, this re-confirms after the `.gitignore` update).

- [ ] **Step 5: Commit**

```bash
git add README.md packages/binnacle-core/README.md .gitignore
git commit -m "$(cat <<'EOF'
docs: rewrite root README as a monorepo landing page

Root README.md is now a short landing page pointing at each package
(and keeps the existing icon/identity). Today's full installation/
usage/API guide moves to packages/binnacle-core/README.md, updated for
the binnacle -> binnacle-core rename (install command, import
statements, doc links, file-path references). .gitignore gains entries
for the JS toolchain entering the repo for the first time.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Per-package `CHANGELOG.md` for `binnacle-core`

**Files:**
- Create: `packages/binnacle-core/CHANGELOG.md`

**Interfaces:**
- None — `binnacle-router`'s and `binnacle-ui`'s `CHANGELOG.md` were already created in Tasks 2 and 3.

There is no existing root `CHANGELOG.md` to fold history from (confirmed absent from the repo prior to this plan) — the spec's migration outline assumed one existed; it doesn't, so this task creates `binnacle-core`'s fresh rather than folding anything.

- [ ] **Step 1: Write `packages/binnacle-core/CHANGELOG.md`**

```markdown
# Changelog

All notable changes to `binnacle-core` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/) and
[SemVer](https://semver.org/).

## [Unreleased]

### Changed

- **Breaking:** package renamed from `binnacle` to `binnacle-core`
  (import name `binnacle` → `binnacle_core`) as part of splitting the
  repo into a monorepo (`packages/binnacle-core`, `packages/binnacle-router`,
  `packages/binnacle-ui`). See
  `docs/adr/0001-monorepo-restructure.md`. No logic change.
```

- [ ] **Step 2: Commit**

```bash
git add packages/binnacle-core/CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs: add binnacle-core CHANGELOG.md

No prior root CHANGELOG.md existed to fold history from -- starting
fresh with the rename itself as the first entry.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Update `GUIDELINES.md` path references for the per-package doc layout

**Files:**
- Modify: `GUIDELINES.md`

**Interfaces:**
- Consumes: Task 4 (the doc paths this task points at must already exist).

Only path references change. The ADR-location text in §5.2 is **explicitly not touched** in this task — that's deferred to a follow-up after this restructuring lands, per explicit direction (see Global Constraints).

- [ ] **Step 1: Update §5 "Source of Truth"**

Find:

```markdown
Authoritative when documents disagree, in this order (initialize these under the `docs/` folder on first use if they do not exist):  

1. **[REQUIREMENTS.md](docs/REQUIREMENTS.md)** — functional and non-functional requirements.  
2. **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** — design decisions, technology choices, extension points, C4 diagrams. Must be consistent with REQUIREMENTS.md.  
3. **[PROJECT.md](docs/PROJECT.md)** — delivery status only (`planned | in-progress | delivered | deferred | cancelled`), each entry linking to a requirement. Create on first use if missing.  
4. **[RUNBOOK.md](docs/RUNBOOK.md)** - lessons learned throughout current project. To be reviewed before a task is being performed to avoid past mistakes.  
```

Replace with:

```markdown
Authoritative when documents disagree, in this order (initialize these under the `docs/` folder on first use if they do not exist):  

0. **[OVERVIEW.md](docs/OVERVIEW.md)** — system-level context across all packages: how they relate, repository layout, and shared tooling decisions. Package-specific requirements/architecture (below) must be consistent with it.
1. **REQUIREMENTS.md** (`docs/<package>/REQUIREMENTS.md`, e.g. [docs/binnacle-core/REQUIREMENTS.md](docs/binnacle-core/REQUIREMENTS.md)) — functional and non-functional requirements for that package.  
2. **ARCHITECTURE.md** (`docs/<package>/ARCHITECTURE.md`, e.g. [docs/binnacle-core/ARCHITECTURE.md](docs/binnacle-core/ARCHITECTURE.md)) — design decisions, technology choices, extension points, C4 diagrams for that package. Must be consistent with its own REQUIREMENTS.md.  
3. **[PROJECT.md](docs/PROJECT.md)** — delivery status only (`planned | in-progress | delivered | deferred | cancelled`), each entry linking to a requirement **and naming its package**. Create on first use if missing.  
4. **[RUNBOOK.md](docs/RUNBOOK.md)** - lessons learned throughout current project (shared across all packages). To be reviewed before a task is being performed to avoid past mistakes.  

Elsewhere in this document, a bare "REQUIREMENTS.md" or "ARCHITECTURE.md" means the relevant package's copy under `docs/<package>/`, resolved by which package the change touches.
```

- [ ] **Step 2: Update the `CHANGELOG.md` line immediately below §5's list**

Find:

```markdown
**CHANGELOG.md** follows [Keep a Changelog](https://keepachangelog.com/) and [SemVer](https://semver.org/). Every merge to main adds an `## [Unreleased]` entry; tagging rolls it into a versioned section.  
```

Replace with:

```markdown
**CHANGELOG.md is per package** (`packages/<name>/CHANGELOG.md`, since each package carries its own independent SemVer version — see §11 Versioning), following [Keep a Changelog](https://keepachangelog.com/) and [SemVer](https://semver.org/). Every merge to main touching a package adds an `## [Unreleased]` entry to that package's changelog; tagging that package's release rolls it into a versioned section.  
```

- [ ] **Step 3: Update §1.1 and §5.1's `docs/components/*` references**

Find (§1.1, first paragraph):

```markdown
The design spec (`docs/components/*`, and REQUIREMENTS) and the phase plans (`docs/superpowers/plans/*`) exist so work is **trackable against an agreed contract**
```

Replace with:

```markdown
The design spec (`docs/<package>/components/*`, and each package's REQUIREMENTS.md) and the phase plans (`docs/superpowers/plans/*`) exist so work is **trackable against an agreed contract**
```

Find (§1.1, bullet list):

```markdown
- **Plan/spec edits themselves** — changing a decision, marking scope done/deferred, or amending a `docs/components/*` file.  
```

Replace with:

```markdown
- **Plan/spec edits themselves** — changing a decision, marking scope done/deferred, or amending a `docs/<package>/components/*` file.  
```

Find (§5.1 "Schema changes update every schema-describing file"):

```markdown
- The affected **component specs** under `docs/components/`, and any other touched spec.  
```

Replace with:

```markdown
- The affected **component specs** under `docs/<package>/components/`, and any other touched spec.  
```

- [ ] **Step 4: Update §11 Definition of Done's doc-update line**

Find:

```markdown
- [ ] REQUIREMENTS.md / ARCHITECTURE.md updated in the same commit if behaviour or design changed.  
```

Replace with:

```markdown
- [ ] The touched package's REQUIREMENTS.md / ARCHITECTURE.md (`docs/<package>/`) updated in the same commit if behaviour or design changed; `docs/OVERVIEW.md` updated too if the change affects cross-package structure.  
```

- [ ] **Step 5: Verify every remaining `docs/REQUIREMENTS.md` / `docs/ARCHITECTURE.md` / `docs/components` literal reference in the file was intentionally left as generic prose, not a stale link**

Run: `grep -n 'docs/REQUIREMENTS.md\|docs/ARCHITECTURE.md\|docs/components' GUIDELINES.md`
Expected: no output — every literal path reference was one of the four replaced above. (Bare mentions of the words "REQUIREMENTS.md"/"ARCHITECTURE.md" without a `docs/` path prefix, e.g. in §8/§9/§10/§11's prose, are untouched by design — §5's new closing sentence from Step 1 already establishes how those resolve.)

- [ ] **Step 6: Commit**

```bash
git add GUIDELINES.md
git commit -m "$(cat <<'EOF'
docs: update GUIDELINES.md paths for the per-package doc layout

REQUIREMENTS.md/ARCHITECTURE.md/components/ references become
docs/<package>/-scoped, with docs/OVERVIEW.md added as the system-level
document sitting above them, and CHANGELOG.md noted as per-package. The
ADR-location text in §5.2 is deliberately NOT touched here -- that's a
separate follow-up after this restructuring lands, per explicit
direction (see docs/superpowers/specs/
2026-09-04-monorepo-restructure-design.md §5.2).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Record this change as an ADR

**Files:**
- Create: `docs/adr/0001-monorepo-restructure.md`

**Interfaces:**
- Consumes: Task 11 (the SemVer bump decision must be known before this ADR can state it as a completed fact — do this task after Task 11).

- [ ] **Step 1: Write `docs/adr/0001-monorepo-restructure.md`**

```markdown
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
  name. [SemVer bump recorded here once Task 11 confirms it.]
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
```

- [ ] **Step 2: Fill in the SemVer bump bracket from Task 11's outcome**

Replace `[SemVer bump recorded here once Task 11 confirms it.]` in the "Consequences" section above with the actual confirmed bump, e.g. `binnacle-core bumped to 0.3.0 with a BREAKING CHANGE footer (pre-1.0, per GUIDELINES §11).` — using whatever Task 11 actually confirmed, not this example verbatim.

- [ ] **Step 3: Commit**

```bash
git add docs/adr/0001-monorepo-restructure.md
git commit -m "$(cat <<'EOF'
docs: record the monorepo restructure as ADR 0001

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Propose and apply the SemVer bump for the breaking rename

**Files:**
- Modify: `packages/binnacle-core/pyproject.toml` (version field)
- Modify: `packages/binnacle-core/CHANGELOG.md` (roll `[Unreleased]` into a versioned section)

**Interfaces:**
- Produces: the confirmed version Task 10 Step 2 records in the ADR.

**This task requires a human-in-the-loop stop — do not pick a number and apply it unilaterally.**

- [ ] **Step 1: STOP. Propose the exact bump to the user and wait for explicit confirmation**

`binnacle-core` is currently at `0.2.0`. GUIDELINES §11 requires proposing the exact bump and getting explicit confirmation before applying it — never silently. State to the user: *"`binnacle` → `binnacle-core` breaks `import binnacle` for any existing caller. Pre-1.0, GUIDELINES §11 allows a breaking change to ride a minor bump with a `BREAKING CHANGE:` footer rather than forcing a major version. Proposing `0.2.0` → `0.3.0` with that footer — confirm, or tell me a different number."*

Do not proceed to Step 2 until the user has explicitly confirmed a number.

- [ ] **Step 2: Apply the confirmed version to `packages/binnacle-core/pyproject.toml`**

Update the `version = "0.2.0"` line under `[project]` to the confirmed value.

- [ ] **Step 3: Roll `CHANGELOG.md`'s `[Unreleased]` section into a versioned entry**

In `packages/binnacle-core/CHANGELOG.md`, change:

```markdown
## [Unreleased]

### Changed

- **Breaking:** package renamed from `binnacle` to `binnacle-core`
```

to:

```markdown
## [Unreleased]

## [<confirmed-version>] - 2026-09-04

### Changed

- **Breaking:** package renamed from `binnacle` to `binnacle-core`
```

(leaving the rest of that bullet's text as already written, and leaving `## [Unreleased]` empty above it for the next change).

- [ ] **Step 4: Verify**

Run: `grep version packages/binnacle-core/pyproject.toml | head -1`
Expected: matches the confirmed version from Step 1.

- [ ] **Step 5: Update the ADR with the confirmed bump**

Go back to Task 10 Step 2 and fill in the bracketed sentence in
`docs/adr/0001-monorepo-restructure.md` now that the version is confirmed
(if Task 10 was done first with a placeholder bracket still in place — if
Task 10 hasn't run yet, do it now with the real number already known,
skipping its own Step 2).

- [ ] **Step 6: Commit**

```bash
git add packages/binnacle-core/pyproject.toml packages/binnacle-core/CHANGELOG.md docs/adr/0001-monorepo-restructure.md
git commit -m "$(cat <<'EOF'
chore(binnacle-core)!: bump version for the binnacle -> binnacle-core rename

BREAKING CHANGE: the distribution is now binnacle-core and the import
is binnacle_core -- `import binnacle` no longer works. Confirmed with
the user per GUIDELINES §11 before applying (see the plan's Task 11).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Notes

- **Spec coverage:** every spec §10 migration-outline item maps to a task above (§10.1–2 → Task 1; §10.3–4 → Task 4; §10.5 → Task 1; §10.6 → Tasks 2–3; §10.7 → Task 3; §10.8 → Tasks 5–6; §10.9 → Task 10; §10.10 → Task 9 (minus the ADR-text deferral per the user's later direction); §10.11 → Task 8 (adjusted: no root CHANGELOG existed to fold — see Task 8's note); §10.12 → Task 7; §10.13 → Task 7; §10.14 → Task 6. DR-6 (breaking change/SemVer) → Task 11. DR-7 (cross-package layering deferral) → stated explicitly in Task 2 and `docs/binnacle-router/ARCHITECTURE.md` (Task 4), not silently dropped.
- **Placeholder scan:** no TBD/TODO left in any step; every code/config block is complete, real content an engineer can run as-is.
- **Type/name consistency checked:** `binnacle_core` (Task 1) is the same import name used in Task 7's README fixes and Task 5's check script; `binnacle_router` (Task 2) matches Task 5's script and Task 4's docs; `binnacle-ui` (Task 3, unhyphenated import not applicable — JS) matches Task 5/6/7 consistently. `packages/binnacle-core/pyproject.toml`'s `root_package = "binnacle_core"` (Task 1) matches the contract module paths in the same file.
- **Ordering dependency called out explicitly:** Task 10 depends on Task 11's outcome (the ADR states the confirmed SemVer bump) — noted in both tasks' Interfaces/Steps so an executor doesn't finalize the ADR with a placeholder bracket still in it.
