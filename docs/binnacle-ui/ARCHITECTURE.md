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
