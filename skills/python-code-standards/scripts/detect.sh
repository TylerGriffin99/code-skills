#!/usr/bin/env bash
# Detect stack, layout, test command and existing guardrails of a Python repo. Read-only.
# Usage: detect.sh <repo>   → KEY: value lines
set -u
REPO="${1:-.}"
cd "$REPO" || { echo "DETECT_ERROR: no such dir $REPO"; exit 2; }

# Source root: a dir containing __init__.py whose parent has pyproject/setup, prefer app/ src/<pkg>/ <pkg>/
SRC=""
for cand in app src/* backend/app backend/src/* .; do
  [ -f "$cand/__init__.py" ] && { SRC="$cand"; break; }
done
[ -z "$SRC" ] && SRC=$(find . -maxdepth 3 -name '__init__.py' -not -path '*/.venv/*' -not -path '*/tests/*' -not -path '*/node_modules/*' | head -1 | xargs -I{} dirname {} | sed 's#^\./##')
echo "SRC_DIR: ${SRC:-unknown}"
ROOT_PKG=$(basename "${SRC:-unknown}")
echo "ROOT_PKG: $ROOT_PKG"
PYPROJECT=$(find . -maxdepth 2 -name pyproject.toml -not -path '*/node_modules/*' | head -1)
echo "PYPROJECT: ${PYPROJECT:-none}"

deps=$(cat ${PYPROJECT:-/dev/null} requirements*.txt 2>/dev/null | tr 'A-Z' 'a-z')
for pkg in fastapi pydantic sqlalchemy httpx pydantic-ai; do
  if printf '%s' "$deps" | grep -q "\"$pkg\|^$pkg\|'$pkg"; then echo "$pkg: yes"; else echo "$pkg: no"; fi
done

layer() { # name, candidates... — a layer may be a package dir or a single module file
  local name=$1; shift
  for d in "$@"; do
    [ -d "$SRC/$d" ] && { echo "LAYER_$name: $SRC/$d"; return; }
    [ -f "$SRC/$d.py" ] && { echo "LAYER_$name: $SRC/$d"; return; }
  done
  echo "LAYER_$name: none"
}
layer ROUTERS routers controllers api/routes api
layer SERVICES services
layer REPOS repos repositories dal
layer MODELS models db/models
layer SCHEMAS schemas
layer CLIENTS clients
[ -d "$SRC/prompts" ] && echo "PROMPTS_DIR: $SRC/prompts" || echo "PROMPTS_DIR: none"

# Test command
if [ -f justfile ] && grep -qE '^test' justfile; then echo "TEST_CMD: just test";
elif [ -f Makefile ] && grep -qE '^test:' Makefile; then echo "TEST_CMD: make test";
elif [ -d tests ] || ls test_*.py >/dev/null 2>&1; then
  if [ -f uv.lock ] || grep -q '^\[tool\.uv\]' pyproject.toml 2>/dev/null; then echo "TEST_CMD: uv run pytest -q"; else echo "TEST_CMD: python -m pytest -q"; fi;
else echo "TEST_CMD: none"; fi
[ -f .env ] && echo "ENV_FILE: .env (tests may need --env-file .env)" || echo "ENV_FILE: none"

# Existing guardrails
grep -q '\[tool.ruff' ${PYPROJECT:-/dev/null} 2>/dev/null && echo "REPO_RUFF_CONFIG: yes" || { [ -f ruff.toml ] && echo "REPO_RUFF_CONFIG: yes (ruff.toml)" || echo "REPO_RUFF_CONFIG: no"; }
grep -qE '\[tool\.(ty|mypy|pyright)' ${PYPROJECT:-/dev/null} 2>/dev/null && echo "REPO_TYPECHECK_CONFIG: yes" || echo "REPO_TYPECHECK_CONFIG: no"
[ -f .pre-commit-config.yaml ] && echo "PRE_COMMIT: yes" || echo "PRE_COMMIT: no"
ls .github/workflows/*.y*ml >/dev/null 2>&1 && echo "CI: yes (.github/workflows)" || { [ -f .gitlab-ci.yml ] && echo "CI: yes (gitlab)" || echo "CI: no"; }
[ -f .importlinter ] || grep -q 'importlinter' ${PYPROJECT:-/dev/null} 2>/dev/null && echo "IMPORT_LINTER: yes" || echo "IMPORT_LINTER: no"
echo "PY_FILES: $(find "${SRC:-.}" -name '*.py' -not -path '*/.venv/*' | wc -l | tr -d ' ')"
echo "DETECT_PROTO: 1"
