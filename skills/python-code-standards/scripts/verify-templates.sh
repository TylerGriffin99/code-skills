#!/usr/bin/env bash
# Maintainer self-test: every template combination installs, passes its tests and passes check.sh.
# Usage: verify-templates.sh [work_dir]   (default: a new temp dir). Needs uv and network for the first install.
set -u -o pipefail
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${1:-$(mktemp -d -t pcs-verify.XXXXXX)}"
T="$SKILL_DIR/templates"
failed=0

verify() { # name, overlay dirs...
  local name=$1; shift
  local dir="$WORK/$name"
  rm -rf "$dir"; mkdir -p "$dir"
  local layer
  for layer in "$@"; do cp -R "$T/$layer/." "$dir/"; done
  printf '== %s (%s) ==\n' "$name" "$*"
  ( cd "$dir" \
    && { [ -z "${EXTRA_DEPS:-}" ] || uv add -q $EXTRA_DEPS; } \
    && uv sync -q \
    && uv run pytest -q 2>&1 | tail -1 ) || { echo "VERIFY_FAIL: $name tests"; failed=1; return; }
  bash "$SKILL_DIR/scripts/check.sh" "$dir" | tail -1 | grep -q 'CHECK_RESULT: clean' \
    || { bash "$SKILL_DIR/scripts/check.sh" "$dir"; echo "VERIFY_FAIL: $name check.sh"; failed=1; }
}

verify core core
EXTRA_DEPS="fastapi uvicorn" verify fastapi core fastapi
EXTRA_DEPS="fastapi uvicorn" verify example core fastapi example-slice
# Canary: plant one violation per tool and assert check.sh reports each one and fails.
# A clean result on the templates means nothing if a check has gone dead.
canary() { # name, base dir
  local dir="$WORK/$1" app="$2" out rc marker
  rm -rf "$dir"; cp -R "$WORK/example" "$dir"
  [ "$app" != app ] && { mkdir -p "$dir/$(dirname "$app")"; mv "$dir/app" "$dir/$app"; }
  printf '== %s ==\n' "$1"
  out=$(bash "$SKILL_DIR/scripts/check.sh" "$dir" 2>&1); rc=$?
  [ "$rc" -eq 0 ] || { printf '%s\n' "$out"; echo "VERIFY_FAIL: $1 is not clean before the canary"; failed=1; return; }
  cat >"$dir/$app/services/canary.py" <<'PY'
from datetime import datetime


def stamp() -> int:
    return str(datetime.now())
PY
  printf 'from app.repos.order_repo import OrderRepo\n\n__all__ = ["OrderRepo"]\n' >"$dir/$app/routes/canary.py"
  out=$(bash "$SKILL_DIR/scripts/check.sh" "$dir" 2>&1); rc=$?
  [ "$rc" -eq 1 ] || { echo "VERIFY_FAIL: $1 check.sh exit $rc, expected 1"; failed=1; }
  for marker in DTZ005 MYPY 'is not allowed to import' CLOCK_NOT_INJECTED; do
    printf '%s\n' "$out" | grep -q "$marker" || { echo "VERIFY_FAIL: $1 missing $marker"; failed=1; }
  done
  out=$(bash "$SKILL_DIR/scripts/check.sh" "$dir" --hook "$dir/$app/services/canary.py" 2>&1 >/dev/null); rc=$?
  [ "$rc" -eq 2 ] || { echo "VERIFY_FAIL: $1 hook exit $rc, expected 2"; failed=1; }
  printf '%s\n' "$out" | grep -q CLOCK_NOT_INJECTED || { echo "VERIFY_FAIL: $1 hook stderr missing CLOCK_NOT_INJECTED"; failed=1; }
  bash "$SKILL_DIR/scripts/check.sh" "$dir" --hook "$dir/$app/services/order_service.py" >/dev/null 2>&1 \
    || { echo "VERIFY_FAIL: $1 hook on a clean file is not clean"; failed=1; }
  echo "canary findings reported"
}

[ "$failed" -eq 0 ] && canary canary app
[ "$failed" -eq 0 ] && canary canary-src-layout src/app

[ "$failed" -eq 0 ] && echo "VERIFY_RESULT: all templates green ($WORK)" || echo "VERIFY_RESULT: failures ($WORK)"
exit "$failed"
