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
[ "$failed" -eq 0 ] && echo "VERIFY_RESULT: all templates green ($WORK)" || echo "VERIFY_RESULT: failures ($WORK)"
exit "$failed"
