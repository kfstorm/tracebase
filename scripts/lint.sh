#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

CHECK_ONLY=false
case "${1:-}" in
"") ;;
--check) CHECK_ONLY=true ;;
*)
  printf 'Usage: %s [--check]\n' "$0" >&2
  exit 2
  ;;
esac

if [[ "${CI:-}" == "true" ]]; then
  CHECK_ONLY=true
fi

if [[ "$CHECK_ONLY" == true ]]; then
  uv run ruff check src tests
  uv run ruff format --check src tests
else
  uv run ruff check --fix src tests
  uv run ruff format src tests
  uv run ruff check src tests
fi

uv run mypy
# Keep test-only references from masking dead code in the production package.
uv run vulture src vulture_whitelist.py
# Then include tests to detect unused test helpers and fixtures.
uv run vulture src tests vulture_whitelist.py
uv run tach check-external
uv run pymarkdown --strict-config scan -r AGENTS.md CONTEXT.md docs
