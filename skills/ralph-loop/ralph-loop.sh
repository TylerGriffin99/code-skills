#!/usr/bin/env bash
# ralph-loop.sh - run fresh-context `claude -p` iterations over a superpowers plan until the
# definition of done is green, the loop is blocked, or a guard trips.
#
# Usage: ralph-loop.sh <run-dir>
#
# <run-dir> is created by the /ralph-loop skill and holds config.env, locks.sha256, logs/ and
# runner.log. Re-running the same command resumes where the loop stopped.
#
# Stop it:  touch <run-dir>/STOP   (finishes the current iteration, then exits)
#
# Exit codes: 0 DONE   3 BLOCKED   4 stalled   5 locked file changed   6 max iterations
#             7 repeated claude errors   8 STOP requested   2 setup error
# Written for macOS bash 3.2.

set -u

RUN_DIR="${1:-}"
[ -n "$RUN_DIR" ] && [ -f "$RUN_DIR/config.env" ] || { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
RUN_DIR=$(cd "$RUN_DIR" && pwd)
SKILL_DIR=$(cd "$(dirname "$0")" && pwd)

# config.env sets: WORKTREE SPEC PLAN CHECKS BASE and optionally MAX_ITER PERMISSION_MODE
# ITER_BUDGET_USD MODEL STALL_LIMIT
# shellcheck disable=SC1091
. "$RUN_DIR/config.env"
MAX_ITER="${MAX_ITER:-40}"
PERMISSION_MODE="${PERMISSION_MODE:-auto}"
STALL_LIMIT="${STALL_LIMIT:-3}"
ITER_BUDGET_USD="${ITER_BUDGET_USD:-}"
MODEL="${MODEL:-}"

RALPH_DIR="$WORKTREE/.ralph"
STATE="$RALPH_DIR/state.md"
LOCKS="$RUN_DIR/locks.sha256"
LOG="$RUN_DIR/runner.log"
mkdir -p "$RUN_DIR/logs"

say() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

notify() {
  command -v osascript >/dev/null 2>&1 &&
    osascript -e "display notification \"$1\" with title \"ralph-loop\"" >/dev/null 2>&1
  return 0
}

finish() { # code message
  say "$2"
  notify "$2"
  exit "$1"
}

check_locks() {
  if ! shasum -a 256 -c "$LOCKS" > "$RUN_DIR/logs/locks-last.txt" 2>&1; then
    grep -v ': OK$' "$RUN_DIR/logs/locks-last.txt" | tee -a "$LOG"
    finish 5 "HALT: a locked file changed ($1). Inspect with: git -C $WORKTREE diff"
  fi
}

# Progress signal: HEAD, ticked plan steps, and the state file minus its `- it<N>` log lines
# (every iteration appends one, so including them would make every iteration look like progress).
# A failing tool halts the loop instead of reading as "no change". Sets FP_NOW; called directly
# (not in $(...)) so that finish exits the runner, not a subshell.
fingerprint() {
  local head ticked state
  head=$(git -C "$WORKTREE" rev-parse HEAD 2>&1) || finish 2 "HALT: git rev-parse failed: $head"
  [ -r "$PLAN" ] || finish 2 "HALT: plan not readable: $PLAN"
  ticked=$(grep -c -- '- \[x\]' "$PLAN")
  [ -r "$STATE" ] || finish 2 "HALT: state file not readable: $STATE"
  state=$(grep -v '^- it[0-9]' "$STATE" | shasum -a 256 | cut -c1-16)
  FP_NOW="$head $ticked $state"
}

render_prompt() { # iteration -> stdout
  local t
  t=$(cat "$SKILL_DIR/iteration.md")
  t="${t//'{{ITER}}'/$1}"
  t="${t//'{{MAX_ITER}}'/$MAX_ITER}"
  t="${t//'{{SPEC}}'/${SPEC:-none; the plan header (Goal, Architecture) is the spec}}"
  t="${t//'{{PLAN}}'/$PLAN}"
  t="${t//'{{STATE}}'/$STATE}"
  t="${t//'{{CHECKS}}'/$CHECKS}"
  t="${t//'{{DOD}}'/$SKILL_DIR/dod.sh}"
  t="${t//'{{RALPH_DIR}}'/$RALPH_DIR}"
  t="${t//'{{BASE}}'/$BASE}"
  printf '%s\n' "$t"
}

cd "$WORKTREE" || finish 2 "setup error: worktree not found: $WORKTREE"
for tool in claude git shasum jq; do
  command -v "$tool" >/dev/null 2>&1 || finish 2 "setup error: $tool not on PATH"
done
[ -f "$PLAN" ] || finish 2 "setup error: plan not found: $PLAN"
[ -f "$LOCKS" ] || finish 2 "setup error: lock manifest not found: $LOCKS"
[ -f "$STATE" ] || finish 2 "setup error: state file not found: $STATE"

ITER=$(cat "$RUN_DIR/iteration" 2>/dev/null || echo 0)
STALLS=0
ERRORS=0
REJECTS=0
fingerprint; LAST_FP="$FP_NOW"
say "start: plan=$PLAN worktree=$WORKTREE resume-after=$ITER max=$MAX_ITER mode=$PERMISSION_MODE"

while :; do
  if [ -f "$RALPH_DIR/DONE" ]; then
    check_locks "before final definition-of-done check"
    say "DONE claimed; runner re-running the definition of done independently"
    DOD_LOG_DIR="$RUN_DIR/logs/dod-final" "$SKILL_DIR/dod.sh" "$CHECKS" --quiet 2>&1 | tee -a "$LOG"
    DOD_RC=${PIPESTATUS[0]}
    if [ "$DOD_RC" = 0 ]; then
      finish 0 "DONE after iteration $ITER, definition of done green. Read $RALPH_DIR/REVIEW.md"
    fi
    rm -f "$RALPH_DIR/DONE"
    REJECTS=$((REJECTS + 1))
    printf -- '- runner: DONE rejected after it%s, dod.sh exit %s; see logs/dod-final\n' "$ITER" "$DOD_RC" >> "$STATE"
    if [ "$REJECTS" -ge 2 ]; then
      {
        echo "# Blocked by runner: DONE claimed twice while the definition of done is red"
        echo
        echo "The loop created DONE, but the runner's own dod.sh run failed (exit $DOD_RC)."
        echo "Failing check logs: $RUN_DIR/logs/dod-final"
      } > "$RALPH_DIR/BLOCKED.md"
      finish 3 "BLOCKED: DONE claimed twice with a red definition of done. See $RALPH_DIR/BLOCKED.md"
    fi
    say "DONE rejected: dod.sh exit $DOD_RC. Loop continues; the next iteration will see red."
  fi
  [ -f "$RALPH_DIR/BLOCKED.md" ] && finish 3 "BLOCKED after iteration $ITER. Read $RALPH_DIR/BLOCKED.md"
  [ -f "$RUN_DIR/STOP" ] && { rm -f "$RUN_DIR/STOP"; finish 8 "STOP requested; stopped after iteration $ITER"; }
  [ "$ITER" -ge "$MAX_ITER" ] && finish 6 "HALT: reached max iterations ($MAX_ITER) without DONE"

  check_locks "before iteration $((ITER + 1))"

  ITER=$((ITER + 1))
  echo "$ITER" > "$RUN_DIR/iteration"
  N=$(printf '%03d' "$ITER")
  render_prompt "$ITER" > "$RUN_DIR/logs/iter-$N.prompt.md"

  set -- -p --output-format json --permission-mode "$PERMISSION_MODE"
  [ -n "$MODEL" ] && set -- "$@" --model "$MODEL"
  [ -n "$ITER_BUDGET_USD" ] && set -- "$@" --max-budget-usd "$ITER_BUDGET_USD"
  set -- "$@" --disallowedTools "Bash(git push:*)" "Bash(gh:*)"

  say "iteration $ITER: running"
  claude "$@" < "$RUN_DIR/logs/iter-$N.prompt.md" \
    > "$RUN_DIR/logs/iter-$N.json" 2> "$RUN_DIR/logs/iter-$N.err"
  RC=$?

  if SUMMARY=$(jq -r '"cost=$" + ((.total_cost_usd // 0) * 100 | round / 100 | tostring) + " turns=" + ((.num_turns // 0) | tostring) + " is_error=" + ((.is_error // false) | tostring)' \
      "$RUN_DIR/logs/iter-$N.json" 2>&1); then
    IS_ERROR=$(jq -r '.is_error // false' "$RUN_DIR/logs/iter-$N.json")
  else
    SUMMARY="output not parseable as JSON: $SUMMARY"
    IS_ERROR=true
  fi
  LAST_LOG=$(grep -E '^- it' "$STATE" 2>/dev/null | tail -1)
  say "iteration $ITER: exit=$RC $SUMMARY"
  [ -n "$LAST_LOG" ] && say "  state: $LAST_LOG"

  if [ "$RC" != 0 ] || [ "$IS_ERROR" != false ]; then
    ERRORS=$((ERRORS + 1))
    say "  iteration failed (exit $RC, is_error $IS_ERROR); stderr tail:"
    tail -n 5 "$RUN_DIR/logs/iter-$N.err" | tee -a "$LOG"
    [ "$ERRORS" -ge 3 ] && finish 7 "HALT: claude failed 3 iterations in a row (see logs/iter-*.err)"
  else
    ERRORS=0
  fi

  check_locks "after iteration $ITER"

  fingerprint; FP="$FP_NOW"
  if [ "$FP" = "$LAST_FP" ] && [ ! -f "$RALPH_DIR/DONE" ] && [ ! -f "$RALPH_DIR/BLOCKED.md" ]; then
    STALLS=$((STALLS + 1))
    say "  no progress (no commit, no ticked step, no state change): $STALLS/$STALL_LIMIT"
    if [ "$STALLS" -ge "$STALL_LIMIT" ]; then
      {
        echo "# Blocked by runner: stalled"
        echo
        echo "$STALL_LIMIT consecutive iterations produced no commit, no newly ticked plan step,"
        echo "and no state change. Last iteration output: $RUN_DIR/logs/iter-$N.json"
      } > "$RALPH_DIR/BLOCKED.md"
      finish 4 "HALT: stalled for $STALL_LIMIT iterations. See $RALPH_DIR/BLOCKED.md"
    fi
  else
    STALLS=0
  fi
  LAST_FP="$FP"
done
