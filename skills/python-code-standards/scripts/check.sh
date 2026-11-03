#!/usr/bin/env bash
# Mechanical scan of a Python repo against the packaged checks. Writes NOTHING into the repo.
# Usage: check.sh <repo> [--hook] [file ...]
#   default : full report (ruff, ruff format, mypy --strict, import-linter contracts, greps), exit 1 on findings
#   --hook  : findings only, on stderr, exit 2 on findings (Claude Code shows exit-2 stderr to the model);
#             no format/mypy; with file arguments also no import-linter
#   file... : restrict ruff and greps output to these files (hook mode on an edited file)
# A tool that fails to run prints a `<tool>: TOOL_ERROR ...` line and fails the check: never a silent clean.
set -u
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUFF_VERSION="${PCS_RUFF_VERSION:-0.16.10}"
MYPY_VERSION="${PCS_MYPY_VERSION:-2.4.1}"
IMPORT_LINTER_VERSION="${PCS_IMPORT_LINTER_VERSION:-2.15}"
REPO="${1:-.}"; shift || true
HOOK=0; FILES=()
for a in "$@"; do case "$a" in --hook) HOOK=1;; *) FILES+=("$a");; esac; done
cd "$REPO" || { echo "CHECK_ERROR: no such dir $REPO"; exit 2; }

# Read detect.sh output as data. Never eval it: directory names come from the scanned repo.
while IFS= read -r line; do
  key="${line%%: *}"; value="${line#*: }"
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && printf -v "$key" '%s' "$value"
done < <(bash "$SKILL_DIR/scripts/detect.sh" .)
SRC="${SRC_DIR:-unknown}"
[ "$SRC" = unknown ] && { echo "CHECK_ERROR: detect.sh found no Python package (SRC_DIR unknown)"; exit 2; }
SRC_PARENT=$(dirname "$SRC")
say() { [ "$HOOK" -eq 1 ] || printf '%s\n' "$1"; }
note() { say "note: $1"; }
present() { [ "${1:-none}" != none ]; }
# Layer path (src/app/routes) -> module name relative to the root package's parent (app.routes).
mod() { local p="$1"; [ "$SRC_PARENT" != . ] && p="${p#"$SRC_PARENT"/}"; printf '%s' "$p" | sed -E 's#/#.#g'; }
# Repo-relative form of a path argument (absolute, ./x or x).
relpath() { local p="$1"; p="${p#"$PWD"/}"; printf '%s' "${p#./}"; }
tool_error() { printf '%s\n' "$2" | tail -5 | sed "s/^/$1: TOOL_ERROR /"; }

run_ruff() {
  local targets=("$SRC") out rc
  [ "${#FILES[@]}" -gt 0 ] && targets=("${FILES[@]}")
  say "== ruff (packaged config: checks/ruff.toml) =="
  out=$(uvx -q "ruff@$RUFF_VERSION" check --no-cache --config "$SKILL_DIR/checks/ruff.toml" --output-format concise "${targets[@]}" 2>&1); rc=$?
  # ruff exits 1 for findings, 2 for its own errors.
  [ "$rc" -ge 2 ] && { tool_error ruff "$out"; return 1; }
  printf '%s\n' "$out" | grep -vE '^Found [0-9]+ error|^\[\*\]|^All checks passed|^No fixes available|^$'
  [ "$rc" -ne 0 ] && return 1
  [ "$HOOK" -eq 1 ] && return 0
  say "== ruff format --check =="
  out=$(uvx -q "ruff@$RUFF_VERSION" format --no-cache --check --config "$SKILL_DIR/checks/ruff.toml" "${targets[@]}" 2>&1); rc=$?
  [ "$rc" -ge 2 ] && { tool_error ruff-format "$out"; return 1; }
  # ruff 0.16 prints "unformatted: File would be reformatted" then "  --> path:line:col"; older ruff prints "Would reformat: path".
  printf '%s\n' "$out" | sed -nE 's/^[[:space:]]*--> ([^:]+):[0-9]+:[0-9]+.*$/\1:1: FORMAT file would be reformatted/p; s/^Would reformat: (.*)$/\1:1: FORMAT file would be reformatted/p' | sort -u
  return "$rc"
}

run_mypy() {
  [ "$HOOK" -eq 1 ] && return 0
  say "== mypy --strict (packaged config: checks/mypy.ini) =="
  if ! present "${VENV_PYTHON:-none}"; then
    echo ".venv:1: TYPECHECK_SKIPPED no interpreter found; run the repo's install (uv sync), or set PCS_PYTHON=/path/to/python"
    return 1
  fi
  # Cache outside the repo; resolve third-party packages from the repo's own interpreter.
  local cache out rc
  cache="${TMPDIR:-/tmp}/pcs-mypy-$(printf '%s' "$PWD" | cksum | cut -d' ' -f1)"
  out=$(uvx -q "mypy@$MYPY_VERSION" --config-file "$SKILL_DIR/checks/mypy.ini" --cache-dir "$cache" \
        --python-executable "$VENV_PYTHON" --no-error-summary "$SRC" 2>&1); rc=$?
  # mypy exits 1 for type errors, 2 for its own errors.
  [ "$rc" -ge 2 ] && { tool_error mypy "$out"; return 1; }
  [ -n "$out" ] && printf '%s\n' "$out" | sed -E 's/^([^:]+:[0-9]+): error: (.*)$/\1: MYPY \2/'
  return "$rc"
}

# Contracts are generated from the layers detect.sh found; a contract naming a missing module would error.
render_contracts() {
  echo "[importlinter]"; echo "root_package = $ROOT_PKG"; echo "include_external_packages = True"
  local layers=() leaves=() uppers=() http_free=() l
  local database="${LAYER_DATABASE:-none}"
  # A models package nested inside the database package (db/models) cannot be a separate layer below it.
  case "${LAYER_MODELS:-none}" in "$database"/*) note "database layer $database contains models; left out of the layers contract" >&2; database=none;; esac
  for l in "${LAYER_ROUTES:-none}" "${LAYER_SERVICES:-none}" "${LAYER_REPOS:-none}" "${LAYER_MODELS:-none}" "$database"; do
    present "$l" && layers+=("$(mod "$l")")
  done
  if [ "${#layers[@]}" -ge 2 ]; then
    printf '\n[importlinter:contract:layers]\nname = Layers: routes -> services -> repos -> models -> database\ntype = layers\nlayers =\n'
    printf '    %s\n' "${layers[@]}"
  else
    note "layers contract skipped (fewer than two of routes/services/repos/models/database detected)" >&2
  fi
  if present "${LAYER_ROUTES:-none}"; then
    printf '\n[importlinter:contract:thin-routes]\nname = Routes reach data only through services\ntype = forbidden\nsource_modules =\n    %s\nforbidden_modules =\n' "$(mod "$LAYER_ROUTES")"
    for l in "${LAYER_REPOS:-none}" "${LAYER_MODELS:-none}" "$database"; do present "$l" && echo "    $(mod "$l")"; done
    if [ "${sqlalchemy:-no}" = yes ]; then echo "    sqlalchemy"; else note "thin-routes: sqlalchemy not detected as a dependency; not forbidden" >&2; fi
    echo "allow_indirect_imports = True"
  fi
  for l in "${LAYER_SCHEMAS:-none}" "${LAYER_CLIENTS:-none}" "${LAYER_UTILS:-none}"; do present "$l" && leaves+=("$(mod "$l")"); done
  uppers=("${layers[@]+"${layers[@]}"}")
  if [ "${#leaves[@]}" -ge 1 ] && [ "${#uppers[@]}" -ge 1 ]; then
    printf '\n[importlinter:contract:leaf-packages]\nname = schemas, clients and utils import no app layer\ntype = forbidden\nsource_modules =\n'
    printf '    %s\n' "${leaves[@]}"; echo "forbidden_modules ="; printf '    %s\n' "${uppers[@]}"
    echo "allow_indirect_imports = True"
  fi
  if [ "${fastapi:-no}" = yes ]; then
    for l in "${LAYER_SERVICES:-none}" "${LAYER_REPOS:-none}" "${LAYER_MODELS:-none}" "$database" \
             "${LAYER_SCHEMAS:-none}" "${LAYER_CLIENTS:-none}" "${LAYER_UTILS:-none}"; do
      present "$l" && http_free+=("$(mod "$l")")
    done
    if [ "${#http_free[@]}" -ge 1 ]; then
      printf '\n[importlinter:contract:http-free]\nname = Only routes, dependencies, main and error_handlers know FastAPI\ntype = forbidden\nsource_modules =\n'
      printf '    %s\n' "${http_free[@]}"; printf 'forbidden_modules =\n    fastapi\n    starlette\nallow_indirect_imports = True\n'
    fi
  else
    note "http-free contract skipped (fastapi not detected as a dependency)" >&2
  fi
}

run_import_linter() {
  # Per-file hook runs skip the whole-repo graph: it is slow and its findings are not about the edited file.
  [ "$HOOK" -eq 1 ] && [ "${#FILES[@]}" -gt 0 ] && return 0
  local cfg out rc
  cfg=$(mktemp -t pcs-importlinter.XXXXXX)
  render_contracts >"$cfg" 2> >([ "$HOOK" -eq 1 ] || cat >&2)
  if ! grep -q 'contract:' "$cfg"; then
    say "== import-linter skipped (no contract applies to the detected layers) =="
    rm -f "$cfg"; return 0
  fi
  say "== import-linter (generated contracts) =="
  # import-linter needs the root package importable: run from the parent of SRC.
  out=$(cd "$SRC_PARENT" && PYTHONPATH="$PWD" uvx -q --from "import-linter==$IMPORT_LINTER_VERSION" lint-imports --no-cache --config "$cfg" 2>&1); rc=$?
  rm -f "$cfg"
  if [ "$rc" -ne 0 ] && ! printf '%s\n' "$out" | grep -q 'BROKEN'; then
    tool_error importlinter "$out"; return 1
  fi
  printf '%s\n' "$out" | grep -E '^- |is not allowed to import|BROKEN|KEPT' | sed 's/^/importlinter: /' \
    | { if [ "$HOOK" -eq 1 ]; then grep -vE 'KEPT'; else cat; fi; }
  return "$rc"
}

run_greps() {
  say "== greps (repo-shape rules) =="
  local out rc
  out=$(bash "$SKILL_DIR/checks/greps.sh" "$SRC" "${LAYER_ROUTES:-}" 2>&1); rc=$?
  # greps.sh exits 1 for findings, 2 for its own errors.
  [ "$rc" -ge 2 ] && { tool_error greps "$out"; return 1; }
  if [ "${#FILES[@]}" -gt 0 ]; then
    # Hook mode on specific files: report only findings whose path equals one of the files (exact match, no regex).
    local f wanted=""
    for f in "${FILES[@]}"; do wanted+="$(relpath "$f")"$'\n'; done
    out=$(printf '%s\n' "$out" | awk -F: 'NR == FNR { want[$0] = 1; next } ($1 in want)' <(printf '%s' "$wanted") -)
    [ -n "$out" ] || return 0
    printf '%s\n' "$out"; return 1
  fi
  [ -n "$out" ] && printf '%s\n' "$out"
  return "$rc"
}

LOG=$(mktemp -t pcs-check.XXXXXX)
trap 'rm -f "$LOG" "$LOG.status"' EXIT
run_all() {
  local status=0
  run_ruff || status=1; run_mypy || status=1; run_import_linter || status=1; run_greps || status=1
  echo "$status" >"$LOG.status"
}
if [ "$HOOK" -eq 1 ]; then
  # Claude Code feeds a hook's stderr back to the model only on exit 2.
  run_all >"$LOG" 2>&1
  status=$(cat "$LOG.status" 2>/dev/null || echo 1)
  [ "$status" -eq 0 ] && exit 0
  sed '/^note: /d' "$LOG" >&2
  exit 2
fi
run_all 2>&1 | tee "$LOG"
status=$(cat "$LOG.status" 2>/dev/null || echo 1)
total=$(grep -cE '^[^=].*:[0-9]+:.*[A-Z][A-Z0-9_]+|^importlinter: -|TOOL_ERROR' "$LOG" || true)
echo "CHECK_FINDINGS: ${total:-0}"
if [ "$status" -eq 0 ]; then echo "CHECK_RESULT: clean"; else echo "CHECK_RESULT: findings (see above)"; fi
exit "$status"
