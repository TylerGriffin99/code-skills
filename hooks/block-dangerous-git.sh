#!/bin/bash
# PreToolUse(Bash): block destructive git commands and git-hook bypasses.
# Based on mattpocock/skills git-guardrails-claude-code, extended to block
# --no-verify / core.hooksPath so agents cannot skip pre-commit checks.

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$COMMAND" ] && exit 0

DANGEROUS_PATTERNS=(
  "push --force"
  "reset --hard"
  "git clean -[a-zA-Z]*f"
  "git branch -D"
  "git checkout \.( |$|;|&)"
  "git restore \.( |$|;|&)"
  "--no-verify"
  "core\.hooksPath"
  "git commit[^|;&]* -[a-zA-Z]*n( |$)"
)

for pattern in "${DANGEROUS_PATTERNS[@]}"; do
  if echo "$COMMAND" | grep -qE -- "$pattern"; then
    echo "BLOCKED: '$COMMAND' matches dangerous pattern '$pattern'. The user has prevented you from doing this. Ask the user to run it themselves if it is really needed." >&2
    exit 2
  fi
done

exit 0
