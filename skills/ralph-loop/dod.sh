#!/usr/bin/env bash
# dod.sh - run a definition-of-done checks file and report every gap.
#
# Usage: dod.sh <checks-file> [--base <ref>] [--quiet]
#
# Runs from the root of the git repo containing the current directory.
# Exit 0 = every check passed (green). Exit 1 = at least one failed (red). Exit 2 = usage error.
# All checks run even after a failure, so one run shows the whole gap list.
#
# Checks file format (one entry per line, '#' starts a comment):
#   name :: shell command          a check; passes when the command exits 0
#   @base <ref>                    base ref for the built-in diff checks (default: main)
#   @allow-test-edits <path>       a test path (file or dir prefix) whose existing lines may change
#   @locks <sha256-file>           verify a `shasum -a 256` manifest (files the loop must not edit)
#
# Built-in checks (always on):
#   tests-not-weakened   no existing line removed from, and no existing file deleted under, a test
#                        path, and no line removed from a Rust file's inline test module (at or
#                        below its first #[cfg(test)]), since the merge-base with @base, unless
#                        covered by @allow-test-edits
#   locks-intact         only when @locks is given
#
# Checks run under `set -o pipefail`, so `cmd | tee log` fails when cmd fails.
# Full output of each check is kept in $DOD_LOG_DIR (default: a fresh temp dir, printed at the end).
# Written for macOS bash 3.2.

set -u

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

CHECKS=""
BASE_OVERRIDE=""
QUIET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --base) [ $# -ge 2 ] || usage; BASE_OVERRIDE="$2"; shift 2 ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) usage ;;
    -*) echo "dod.sh: unknown option $1" >&2; usage ;;
    *) [ -z "$CHECKS" ] || usage; CHECKS="$1"; shift ;;
  esac
done
[ -n "$CHECKS" ] || usage
[ -f "$CHECKS" ] || { echo "dod.sh: checks file not found: $CHECKS" >&2; exit 2; }
case "$CHECKS" in /*) ;; *) CHECKS="$(pwd)/$CHECKS" ;; esac

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "dod.sh: not inside a git repo" >&2; exit 2; }
cd "$ROOT" || exit 2

LOG_DIR="${DOD_LOG_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/dod.XXXXXX")}"
mkdir -p "$LOG_DIR" || { echo "dod.sh: cannot create log dir $LOG_DIR" >&2; exit 2; }

trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; s="${s%"${s##*[![:space:]]}"}"; printf '%s' "$s"; }

# ---- parse -----------------------------------------------------------------------------------
BASE="main"
LOCKS=""
ALLOW_FILE="$LOG_DIR/.allow-test-edits"
CHECK_LIST="$LOG_DIR/.checks"
: > "$ALLOW_FILE"
: > "$CHECK_LIST"
LINE_NO=0
PARSE_ERR=0
while IFS= read -r raw || [ -n "$raw" ]; do
  LINE_NO=$((LINE_NO + 1))
  line=$(trim "$raw")
  case "$line" in ''|'#'*) continue ;; esac
  case "$line" in
    '@base '*) BASE=$(trim "${line#@base }") ;;
    '@allow-test-edits '*) trim "${line#@allow-test-edits }" >> "$ALLOW_FILE"; echo >> "$ALLOW_FILE" ;;
    '@locks '*) LOCKS=$(trim "${line#@locks }") ;;
    '@'*) echo "dod.sh: $CHECKS:$LINE_NO: unknown directive: $line" >&2; PARSE_ERR=1 ;;
    *' :: '*)
      name=$(trim "${line%% :: *}")
      cmd=$(trim "${line#* :: }")
      if [ -z "$name" ] || [ -z "$cmd" ]; then
        echo "dod.sh: $CHECKS:$LINE_NO: empty name or command" >&2; PARSE_ERR=1
      else
        printf '%s\t%s\n' "$name" "$cmd" >> "$CHECK_LIST"
      fi ;;
    *) echo "dod.sh: $CHECKS:$LINE_NO: expected 'name :: command' or a directive, got: $line" >&2; PARSE_ERR=1 ;;
  esac
done < "$CHECKS"
[ "$PARSE_ERR" = 0 ] || exit 2
[ -n "$BASE_OVERRIDE" ] && BASE="$BASE_OVERRIDE"

# ---- run -------------------------------------------------------------------------------------
PASSED=0
FAILED=0
FAILED_NAMES=""
N=0

record() { # name status logfile
  if [ "$2" = 0 ]; then
    PASSED=$((PASSED + 1)); echo "PASS  $1"
  else
    FAILED=$((FAILED + 1)); FAILED_NAMES="$FAILED_NAMES $1"
    echo "FAIL  $1   (exit $2, log: $3)"
    if [ "$QUIET" = 0 ]; then tail -n 25 "$3" | sed 's/^/      | /'; fi
  fi
}

slug() { printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'; }

is_test_path() {
  printf '%s\n' "$1" | grep -qE '(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.(py|rs|go)$|\.(test|spec)\.[cm]?[jt]sx?$'
}

is_allowed() {
  local p="$1" a
  while IFS= read -r a; do
    [ -z "$a" ] && continue
    a="${a%/}"
    case "$p" in "$a"|"$a"/*) return 0 ;; esac
  done < "$ALLOW_FILE"
  return 1
}

# Count old-side lines removed at or below the first inline test marker of a source file
# (Rust `#[cfg(test)]`, which by convention opens the trailing test module). Prints 0 for files
# without a marker. Returns non-zero if git fails, so a git error never reads as "nothing removed".
inline_test_lines_removed() {
  local path="$1" old marker hunks
  case "$path" in *.rs) ;; *) echo 0; return 0 ;; esac
  old=$(git show "$MB:$path" 2>&1) || { echo "$old"; return 1; }
  marker=$(printf '%s\n' "$old" | grep -n '#\[cfg(test)\]' | head -1 | cut -d: -f1)
  [ -n "$marker" ] || { echo 0; return 0; }
  hunks=$(git diff -U0 --no-renames "$MB" -- "$path" 2>&1) || { echo "$hunks"; return 1; }
  printf '%s\n' "$hunks" | awk -v m="$marker" '
    /^@@ / {
      n_o = split($2, o, ","); start = substr(o[1], 2) + 0; cnt = (n_o > 1) ? o[2] + 0 : 1
      for (i = 0; i < cnt; i++) if (start + i >= m) n++
    }
    END { print n + 0 }'
}

# built-in: tests-not-weakened
LOG="$LOG_DIR/00_tests-not-weakened.log"
(
  if ! MB=$(git merge-base "$BASE" HEAD 2>&1); then
    echo "cannot find merge-base of '$BASE' and HEAD: $MB"
    echo "set '@base <ref>' in the checks file or pass --base"
    exit 3
  fi
  echo "comparing working tree against merge-base $MB ($BASE)"
  # --no-renames: a moved test file shows as delete + add, so a rename cannot hide deletions.
  if ! NUMSTAT=$(git diff --numstat --no-renames "$MB" -- 2>&1); then
    echo "git diff failed; cannot verify tests were not weakened:"
    echo "$NUMSTAT"
    exit 3
  fi
  bad=0
  while IFS="$(printf '\t')" read -r added deleted path; do
    [ -z "$path" ] && continue
    [ "$deleted" = "-" ] && continue          # binary
    [ "$deleted" -gt 0 ] 2>/dev/null || continue
    git cat-file -e "$MB:$path" 2>/dev/null || continue   # file is new on this branch
    if is_test_path "$path"; then
      removed="$deleted"
    else
      # Inline tests: removed lines at or below the file's first test marker.
      if ! removed=$(inline_test_lines_removed "$path"); then
        echo "cannot inspect inline tests in $path: $removed"
        exit 3
      fi
      [ "$removed" -gt 0 ] || continue
    fi
    if is_allowed "$path"; then
      echo "allowed: $path (-$removed test lines, covered by @allow-test-edits)"
      continue
    fi
    echo "WEAKENED: $path has $removed existing test line(s) removed or changed"
    bad=1
  done <<NUMSTAT_END
$NUMSTAT
NUMSTAT_END
  if [ "$bad" = 1 ]; then
    echo
    echo "Existing tests must stay intact. Restore them, or, if the spec deliberately changes"
    echo "that behaviour, a human adds '@allow-test-edits <path>' to the checks file."
    exit 1
  fi
  echo "no existing test lines removed"
) > "$LOG" 2>&1
record "tests-not-weakened" $? "$LOG"

# built-in: locks-intact
if [ -n "$LOCKS" ]; then
  LOG="$LOG_DIR/00_locks-intact.log"
  if [ -f "$LOCKS" ]; then
    shasum -a 256 -c "$LOCKS" > "$LOG" 2>&1
    record "locks-intact" $? "$LOG"
  else
    echo "lock manifest not found: $LOCKS" > "$LOG"
    record "locks-intact" 1 "$LOG"
  fi
fi

# user checks
while IFS="$(printf '\t')" read -r name cmd; do
  [ -z "$name" ] && continue
  N=$((N + 1))
  LOG="$LOG_DIR/$(printf '%02d' "$N")_$(slug "$name").log"
  { echo "\$ $cmd"; echo; } > "$LOG"
  ( cd "$ROOT" && bash -c "set -o pipefail; $cmd" ) >> "$LOG" 2>&1 < /dev/null
  record "$name" $? "$LOG"
done < "$CHECK_LIST"

# ---- summary ---------------------------------------------------------------------------------
echo
if [ "$FAILED" = 0 ]; then
  echo "DOD GREEN: $PASSED/$PASSED checks passed   (logs: $LOG_DIR)"
  exit 0
fi
echo "DOD RED: $FAILED failed, $PASSED passed   failed:$FAILED_NAMES   (logs: $LOG_DIR)"
exit 1
