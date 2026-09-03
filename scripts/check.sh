#!/usr/bin/env bash
set -euo pipefail

uv run ruff format --check src tests
uv run ruff check src tests
# `mypy src` is the strict-mode contract; `tests/` isn't otherwise type-checked.
# `tests/unit/test_typing_narrowing.py` is the one deliberate, narrow exception:
# it exists solely to guard relevant()'s @overload narrowing (Binnacle/StorePort/
# PostgresStore) with `typing.assert_type`, which only fails anything under mypy
# (a runtime no-op otherwise) -- so it must actually run under mypy to guard
# anything. See that file's module docstring.
uv run mypy src tests/unit/test_typing_narrowing.py
uv run lint-imports
uv run pytest -q
