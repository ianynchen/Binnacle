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
