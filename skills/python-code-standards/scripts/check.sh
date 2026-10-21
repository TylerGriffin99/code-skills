#!/usr/bin/env bash
# Mechanical scan of a Python repo against the packaged checks. Writes NOTHING into the repo.
# Usage: check.sh <repo> [--hook] [file ...]
#   default : full report (ruff, import-linter layering, greps), summary line, exit 1 on findings
#   --hook  : findings only (no headers), exit 1 on findings — for Claude Code PostToolUse/Stop hooks
#   file... : restrict ruff to these files (hook mode on an edited file)
set -u
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUFF_VERSION="${PCS_RUFF_VERSION:-0.16.10}"
REPO="${1:-.}"; shift || true
HOOK=0; FILES=()
for a in "$@"; do case "$a" in --hook) HOOK=1;; *) FILES+=("$a");; esac; done
cd "$REPO" || { echo "CHECK_ERROR: no such dir $REPO"; exit 2; }
LOG=$(mktemp -t pcs-check.XXXXXX); exec 3>&1; exec > >(tee "$LOG" >&3)

eval "$(bash "$SKILL_DIR/scripts/detect.sh" . | sed -nE 's/^([A-Z_]+): (.*)$/\1="\2"/p')"
SRC="${SRC_DIR:-.}"
status=0
say() { [ "$HOOK" -eq 1 ] || printf '%s\n' "$1"; }

say "== ruff (packaged config: checks/ruff.toml — TID251/PLC0415/BLE001, line length per ethos) =="
if [ "${#FILES[@]}" -gt 0 ]; then targets=("${FILES[@]}"); else targets=("$SRC"); fi
if ! uvx "ruff@$RUFF_VERSION" check --config "$SKILL_DIR/checks/ruff.toml" --output-format concise "${targets[@]}" 2>&1 | grep -vE '^Found [0-9]+ error|^\[\*\]|^All checks passed'; then :; fi
uvx "ruff@$RUFF_VERSION" check --config "$SKILL_DIR/checks/ruff.toml" -q "${targets[@]}" >/dev/null 2>&1 || status=1
if [ "$HOOK" -eq 0 ]; then
  say "== ruff format --check =="
  # ruff 0.16 prints "unformatted: File would be reformatted" followed by "  --> path:line:col"; older ruff prints "Would reformat: path".
  uvx "ruff@$RUFF_VERSION" format --check --config "$SKILL_DIR/checks/ruff.toml" "${targets[@]}" 2>&1 \
    | sed -nE 's/^[[:space:]]*--> ([^:]+):[0-9]+:[0-9]+.*$/\1:1: FORMAT file would be reformatted/p; s/^Would reformat: (.*)$/\1:1: FORMAT file would be reformatted/p' | sort -u || true
  uvx "ruff@$RUFF_VERSION" format --check --config "$SKILL_DIR/checks/ruff.toml" -q "${targets[@]}" >/dev/null 2>&1 || status=1
fi

# import-linter: only when all four layers were detected (otherwise the contract is meaningless).
if [ "${LAYER_ROUTERS:-none}" != none ] && [ "${LAYER_SERVICES:-none}" != none ] && [ "${LAYER_REPOS:-none}" != none ] && [ "${LAYER_MODELS:-none}" != none ]; then
  say "== import-linter (generated layering contract) =="
  mod() { printf '%s' "$1" | sed -E 's#/#.#g'; }
  tmp=$(mktemp -t pcs-importlinter.XXXXXX)
  sed -e "s#{{ROOT}}#$ROOT_PKG#g" -e "s#{{ROUTERS}}#$(mod "$LAYER_ROUTERS")#g" -e "s#{{SERVICES}}#$(mod "$LAYER_SERVICES")#g" \
      -e "s#{{REPOS}}#$(mod "$LAYER_REPOS")#g" -e "s#{{MODELS}}#$(mod "$LAYER_MODELS")#g" \
      "$SKILL_DIR/checks/importlinter.ini.tmpl" > "$tmp"
  # import-linter needs the root package importable: run from the parent of SRC.
  out=$(cd "$(dirname "$SRC")" 2>/dev/null && PYTHONPATH="$PWD" uvx --from import-linter lint-imports --config "$tmp" 2>&1)
  rc=$?
  printf '%s\n' "$out" | grep -E '^- |is not allowed to import|BROKEN|KEPT' | grep -v '^\s*$' | sed 's/^/importlinter: /' | { [ "$HOOK" -eq 1 ] && grep -vE 'KEPT' || cat; }
  [ $rc -ne 0 ] && status=1
  rm -f "$tmp"
else
  say "== import-linter skipped (layers not all detected: routers=${LAYER_ROUTERS:-none} services=${LAYER_SERVICES:-none} repos=${LAYER_REPOS:-none} models=${LAYER_MODELS:-none}) =="
fi

say "== greps (repo-shape rules) =="
if [ "${#FILES[@]}" -gt 0 ]; then
  # Hook mode on specific files: run the repo-wide greps but report only lines about those files.
  pat=$(printf '%s\n' "${FILES[@]}" | sed -E 's#^\./##; s#^.*/('"$(basename "$SRC")"'/)#\1#' | paste -sd'|' -)
  grep_out=$(bash "$SKILL_DIR/checks/greps.sh" "$SRC" "${LAYER_ROUTERS:-}")
  filtered=$(printf '%s\n' "$grep_out" | grep -E "^($pat)" || true)
  [ -n "$filtered" ] && { printf '%s\n' "$filtered"; status=1; }
else
  bash "$SKILL_DIR/checks/greps.sh" "$SRC" "${LAYER_ROUTERS:-}" || status=1
fi

sleep 0.2  # let tee flush before counting
total=$(grep -cE '^[^=].*:[0-9]+:.*[A-Z][A-Z0-9_]+|^importlinter: -' "$LOG" 2>/dev/null || echo 0)
if [ "$HOOK" -eq 0 ]; then
  echo "CHECK_FINDINGS: $total"
  [ $status -eq 0 ] && echo "CHECK_RESULT: clean" || echo "CHECK_RESULT: findings (see above)"
fi
rm -f "$LOG"
exit $status
