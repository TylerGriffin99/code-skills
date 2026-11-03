#!/usr/bin/env bash
# Mechanical scan of a Python repo against the packaged checks. Writes NOTHING into the repo.
# Usage: check.sh <repo> [--hook] [file ...]
#   default : full report (ruff, ruff format, mypy --strict, import-linter contracts, greps), exit 1 on findings
#   --hook  : findings only (no headers, no format/mypy), exit 1 on findings — for Claude Code PostToolUse/Stop hooks
#   file... : restrict ruff and greps output to these files (hook mode on an edited file)
set -u
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUFF_VERSION="${PCS_RUFF_VERSION:-0.16.10}"
REPO="${1:-.}"; shift || true
HOOK=0; FILES=()
for a in "$@"; do case "$a" in --hook) HOOK=1;; *) FILES+=("$a");; esac; done
cd "$REPO" || { echo "CHECK_ERROR: no such dir $REPO"; exit 2; }

eval "$(bash "$SKILL_DIR/scripts/detect.sh" . | sed -nE 's/^([A-Za-z_]+): (.*)$/\1="\2"/p')"
SRC="${SRC_DIR:-.}"
say() { [ "$HOOK" -eq 1 ] || printf '%s\n' "$1"; }
present() { [ "${1:-none}" != none ]; }
mod() { printf '%s' "$1" | sed -E 's#/#.#g'; }

run_ruff() {
  local targets=("$SRC"); [ "${#FILES[@]}" -gt 0 ] && targets=("${FILES[@]}")
  local rc=0
  say "== ruff (packaged config: checks/ruff.toml) =="
  uvx -q "ruff@$RUFF_VERSION" check --no-cache --config "$SKILL_DIR/checks/ruff.toml" --output-format concise "${targets[@]}" 2>&1 \
    | grep -vE '^Found [0-9]+ error|^\[\*\]|^All checks passed|^No fixes available'
  uvx -q "ruff@$RUFF_VERSION" check --no-cache --config "$SKILL_DIR/checks/ruff.toml" -q "${targets[@]}" >/dev/null 2>&1 || rc=1
  [ "$HOOK" -eq 1 ] && return $rc
  say "== ruff format --check =="
  # ruff 0.16 prints "unformatted: File would be reformatted" then "  --> path:line:col"; older ruff prints "Would reformat: path".
  uvx -q "ruff@$RUFF_VERSION" format --no-cache --check --config "$SKILL_DIR/checks/ruff.toml" "${targets[@]}" 2>&1 \
    | sed -nE 's/^[[:space:]]*--> ([^:]+):[0-9]+:[0-9]+.*$/\1:1: FORMAT file would be reformatted/p; s/^Would reformat: (.*)$/\1:1: FORMAT file would be reformatted/p' | sort -u
  uvx -q "ruff@$RUFF_VERSION" format --no-cache --check --config "$SKILL_DIR/checks/ruff.toml" -q "${targets[@]}" >/dev/null 2>&1 || rc=1
  return $rc
}

run_mypy() {
  [ "$HOOK" -eq 1 ] && return 0
  say "== mypy --strict (packaged config: checks/mypy.ini) =="
  if ! present "${VENV_PYTHON:-none}"; then
    echo ".venv:1: TYPECHECK_SKIPPED no .venv/bin/python; run the repo's install (uv sync) so mypy can resolve imports"
    return 1
  fi
  # Cache outside the repo; resolve third-party packages from the repo's own interpreter.
  local cache="${TMPDIR:-/tmp}/pcs-mypy-$(printf '%s' "$PWD" | cksum | cut -d' ' -f1)"
  local out rc
  out=$(uvx -q mypy --config-file "$SKILL_DIR/checks/mypy.ini" --cache-dir "$cache" --python-executable "$VENV_PYTHON" \
        --no-error-summary "$SRC" 2>&1); rc=$?
  [ -n "$out" ] && printf '%s\n' "$out" | sed -E 's/^([^:]+:[0-9]+): error: (.*)$/\1: MYPY \2/'
  return $rc
}

# Contracts are generated from the layers detect.sh found; a contract naming a missing module would error.
render_contracts() {
  echo "[importlinter]"; echo "root_package = $ROOT_PKG"; echo "include_external_packages = True"
  local layers=() l
  for l in "${LAYER_ROUTES:-none}" "${LAYER_SERVICES:-none}" "${LAYER_REPOS:-none}" "${LAYER_MODELS:-none}" "${LAYER_DATABASE:-none}"; do
    present "$l" && layers+=("$(mod "$l")")
  done
  if [ "${#layers[@]}" -ge 2 ]; then
    printf '\n[importlinter:contract:layers]\nname = Layers: routes -> services -> repos -> models -> database\ntype = layers\nlayers =\n'
    printf '    %s\n' "${layers[@]}"
  fi
  if present "${LAYER_ROUTES:-none}"; then
    printf '\n[importlinter:contract:thin-routes]\nname = Routes reach data only through services\ntype = forbidden\nsource_modules =\n    %s\nforbidden_modules =\n' "$(mod "$LAYER_ROUTES")"
    for l in "${LAYER_REPOS:-none}" "${LAYER_MODELS:-none}" "${LAYER_DATABASE:-none}"; do present "$l" && echo "    $(mod "$l")"; done
    [ "${sqlalchemy:-no}" = yes ] && echo "    sqlalchemy"
    echo "allow_indirect_imports = True"
  fi
  local leaves=() uppers=()
  for l in "${LAYER_SCHEMAS:-none}" "${LAYER_CLIENTS:-none}" "${LAYER_UTILS:-none}"; do present "$l" && leaves+=("$(mod "$l")"); done
  for l in "${LAYER_ROUTES:-none}" "${LAYER_SERVICES:-none}" "${LAYER_REPOS:-none}" "${LAYER_MODELS:-none}" "${LAYER_DATABASE:-none}"; do
    present "$l" && uppers+=("$(mod "$l")")
  done
  if [ "${#leaves[@]}" -ge 1 ] && [ "${#uppers[@]}" -ge 1 ]; then
    printf '\n[importlinter:contract:leaf-packages]\nname = schemas, clients and utils import no app layer\ntype = forbidden\nsource_modules =\n'
    printf '    %s\n' "${leaves[@]}"; echo "forbidden_modules ="; printf '    %s\n' "${uppers[@]}"
    echo "allow_indirect_imports = True"
  fi
  if [ "${fastapi:-no}" = yes ]; then
    local http_free=()
    for l in "${LAYER_SERVICES:-none}" "${LAYER_REPOS:-none}" "${LAYER_MODELS:-none}" "${LAYER_DATABASE:-none}" \
             "${LAYER_SCHEMAS:-none}" "${LAYER_CLIENTS:-none}" "${LAYER_UTILS:-none}"; do
      present "$l" && http_free+=("$(mod "$l")")
    done
    if [ "${#http_free[@]}" -ge 1 ]; then
      printf '\n[importlinter:contract:http-free]\nname = Only routes, dependencies, main and error_handlers know FastAPI\ntype = forbidden\nsource_modules =\n'
      printf '    %s\n' "${http_free[@]}"; printf 'forbidden_modules =\n    fastapi\n    starlette\nallow_indirect_imports = True\n'
    fi
  fi
}

run_import_linter() {
  local cfg out rc
  cfg=$(mktemp -t pcs-importlinter.XXXXXX)
  render_contracts >"$cfg"
  if ! grep -q 'contract:' "$cfg"; then
    say "== import-linter skipped (fewer than two layers detected) =="
    rm -f "$cfg"; return 0
  fi
  say "== import-linter (generated contracts) =="
  # import-linter needs the root package importable: run from the parent of SRC.
  out=$(cd "$(dirname "$SRC")" 2>/dev/null && PYTHONPATH="$PWD" uvx -q --from import-linter lint-imports --no-cache --config "$cfg" 2>&1); rc=$?
  printf '%s\n' "$out" | grep -E '^- |is not allowed to import|BROKEN|KEPT|Error|error' | sed 's/^/importlinter: /' \
    | { [ "$HOOK" -eq 1 ] && grep -vE 'KEPT' || cat; }
  rm -f "$cfg"
  return $rc
}

run_greps() {
  say "== greps (repo-shape rules) =="
  local out rc
  out=$(bash "$SKILL_DIR/checks/greps.sh" "$SRC" "${LAYER_ROUTES:-}"); rc=$?
  if [ "${#FILES[@]}" -gt 0 ]; then
    # Hook mode on specific files: run the repo-wide greps but report only lines about those files.
    local pat
    pat=$(printf '%s\n' "${FILES[@]}" | sed -E 's#^\./##; s#^.*/('"$(basename "$SRC")"'/)#\1#' | paste -sd'|' -)
    out=$(printf '%s\n' "$out" | grep -E "^($pat)" || true)
    [ -n "$out" ] && { printf '%s\n' "$out"; return 1; }
    return 0
  fi
  [ -n "$out" ] && printf '%s\n' "$out"
  return $rc
}

LOG=$(mktemp -t pcs-check.XXXXXX)
trap 'rm -f "$LOG"' EXIT
status=0
{ run_ruff || status=1; run_mypy || status=1; run_import_linter || status=1; run_greps || status=1; echo "$status" >"$LOG.status"; } \
  2>&1 | tee "$LOG"
status=$(cat "$LOG.status" 2>/dev/null || echo 1); rm -f "$LOG.status"
if [ "$HOOK" -eq 0 ]; then
  total=$(grep -cE '^[^=].*:[0-9]+:.*[A-Z][A-Z0-9_]+|^importlinter: -' "$LOG" || true)
  echo "CHECK_FINDINGS: ${total:-0}"
  [ "$status" -eq 0 ] && echo "CHECK_RESULT: clean" || echo "CHECK_RESULT: findings (see above)"
fi
exit "$status"
