#!/usr/bin/env bash
set -euo pipefail

echo "Installing Python dependencies..."
uv sync --all-packages

echo "Installing JS dependencies..."
pnpm install

echo "Installing git hooks (pre-commit AND pre-push stages)..."
uv run pre-commit install --hook-type pre-commit --hook-type pre-push

echo "Done. 'git commit' runs fast checks (gitleaks, ruff, biome);"
echo "'git push' runs the full gate (mypy, import-linter, all tests, ui build)."
