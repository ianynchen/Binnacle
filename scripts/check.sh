#!/usr/bin/env bash
set -euo pipefail

uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run lint-imports
uv run pytest -q
