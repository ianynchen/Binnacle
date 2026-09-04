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
