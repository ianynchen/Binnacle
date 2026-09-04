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
