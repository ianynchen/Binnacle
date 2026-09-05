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

## Theming

Decided during brainstorming (2026-09-04), binding on the eventual
component implementation. **Nothing below is implemented yet** — no
`tokens.css`, no `ThemeProvider`, no component styles exist in the
package. UI/visual design (what the components actually look like,
layout, the component inventory itself) is still to be done; this
section fixes the *mechanism* by which that future design becomes
themeable, not the design itself.

- **Styling mechanism: CSS Modules.** Native to Vite, zero extra
  dependency. Each component gets a scoped `Component.module.css` file.
  Rejected for now: Tailwind, styled-components/emotion — both add a real
  dependency this decision doesn't need yet.
- **Every visual value (color, font, font-size, background) is a CSS
  custom property reference — never a literal** in any component's
  styles. Layout (spacing, flex/grid structure, sizing) may be hardcoded
  for now; only visual/theme values are required to go through a
  variable. This is what makes re-theming later (including a dark mode)
  a matter of changing variable *values*, never component code.
- **All tokens are namespaced `--binnacle-*`** (e.g. `--binnacle-color-bg`,
  `--binnacle-color-accent`), never a bare generic name. CSS custom
  properties cascade globally with no built-in isolation between
  libraries — if a sibling project (e.g. `portolan`'s own UI library)
  independently picked the same bare name for an unrelated purpose, and
  both were mounted in the same host (Meridian), whichever is closer in
  the DOM would silently win for both. The `binnacle-`/`portolan-` prefix
  makes that collision structurally impossible regardless of mounting
  order or nesting.
- **`binnacle-ui` ships the CSS; a host provides only token *values*,
  never new CSS.** The package exports both the component styles and a
  default `tokens.css` (a complete, working light theme) so a host that
  does nothing still gets a fully-styled result. A host wanting a
  different look only ever redefines the same `--binnacle-*` variable
  names — it never needs to know `binnacle-ui`'s internal class names or
  DOM structure.
- **Two ways a host supplies override values**, both valid, host's
  choice: (a) plain CSS redefining the variables under its own `:root`
  scope — the zero-code path; (b) `BinnacleThemeProvider`, an exported
  component taking a partial, typed theme object as props and applying
  it as inline CSS variables on a wrapping element — scoped/dynamic
  overrides (e.g. per-tenant theming at runtime) that plain CSS can't
  express. `BinnacleThemeProvider` is additive: an unset field falls
  through to `tokens.css`'s default via normal CSS inheritance, so a host
  never has to override every variable just to change one.
- **Composing with a sibling library's own ThemeProvider** (e.g. a future
  `PortolanThemeProvider`): safe in any nesting order, since the two
  libraries' variable namespaces never overlap. A host wanting one
  consistent brand across multiple embedded libraries doesn't need to
  render either Provider at all — a single global CSS file overriding
  both `--binnacle-*` and `--portolan-*` achieves that more simply than
  wrapping the tree in two Providers.
