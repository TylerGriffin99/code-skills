#!/usr/bin/env bash
# Repo-shape checks that no linter expresses. Usage: greps.sh <src_dir> [routes_dir]
# Prints one finding per line as  path:line: RULE message  and exits 1 if any were found.
set -u
SRC="${1:?src dir}"
ROUTES="${2:-}"
# Findings go to a temp file: emit often runs in a pipeline subshell, where a shell variable would be lost.
OUT=$(mktemp -t pcs-greps.XXXXXX)
trap 'rm -f "$OUT"' EXIT
emit() { printf '%s\n' "$1" >>"$OUT"; }
each() { # rule message — read grep -n lines from stdin and emit one finding per line
  while IFS= read -r l; do emit "${l%%:*}:$(echo "$l" | cut -d: -f2): $1 $2"; done
}
# Collaborators may be constructed only in the composition root and the providers.
ROOT_FILES='/main\.py:|/dependencies\.py:|/bootstrap\.py:'
# Business-code directories (where logic lives and where injected collaborators are expected).
BUSINESS='/(services?|routes|routers?|controllers?)/'

# 1. Prompt loading by path arithmetic outside a prompts package.
grep -rn --include='*.py' -E 'Path\(__file__\).*prompt|read_text\(\)' "$SRC" 2>/dev/null \
  | grep -vE '/prompts/__init__\.py:|/prompts\.py:' \
  | each PROMPT_PATH_HACK "load prompts via the prompts package's load_prompt()"

# 2. Nested async closure + nonlocal (shared mutable state under gather).
grep -rn --include='*.py' -E '^\s{8,}async def |^\s+nonlocal ' "$SRC" 2>/dev/null \
  | each NESTED_ASYNC_CLOSURE "extract to utils/concurrency.bounded_gather + a function returning an immutable value"

# 3. Module-global client/transport/session (monkeypatch seam) instead of constructor injection.
grep -rn --include='*.py' -E '^_?(transport|client|http_client|session)\s*[:=]' "$SRC" 2>/dev/null \
  | each MODULE_GLOBAL_SEAM "build it once in the lifespan and inject it through a constructor"

# 4. Private helper sprawl: >= 3 leading-underscore defs in one module (dunders excluded).
for f in $(grep -rl --include='*.py' -E '^\s*(async )?def _[a-z]' "$SRC" 2>/dev/null); do
  c=$(grep -cE '^\s*(async )?def _[a-z]' "$f")
  [ "$c" -ge 3 ] && emit "$f:1: PRIVATE_HELPER_SPRAWL $c leading-underscore defs; name and test them, or inline"
done

# 5. Routes doing non-controller work, or hard-coding the API version.
if [ -n "$ROUTES" ] && [ -e "$ROUTES" ]; then
  grep -rn --include='*.py' -E '^\s+return \{|\.commit\(\)|select\(|\.execute\(' "$ROUTES" 2>/dev/null \
    | each FAT_ROUTE "a route calls one service method and returns a DTO"
  grep -rn --include='*.py' -E 'prefix="/api/' "$ROUTES" 2>/dev/null \
    | each HARDCODED_API_PREFIX "mount under settings.api_prefix; routes declare domain-relative prefixes"
fi

# 6. Transaction ownership: the service commits once; repos flush, routes never touch the session.
#    Override the owner paths with PCS_COMMIT_OWNERS (ERE path fragment).
OWNERS="${PCS_COMMIT_OWNERS:-/services?/}"
grep -rn --include='*.py' -E '\.commit\(\)' "$SRC" 2>/dev/null \
  | grep -vE "$OWNERS|/database/|/database\.py:|/db\.py:|/tests?/" \
  | each COMMIT_OUTSIDE_SERVICE "the service commits once at the end of the use case; repos flush"

# 7. Literal string sets in business code: `x in ("a", "b")` is a missing StrEnum (ruff PLR2004 covers `==`).
grep -rn --include='*.py' -E '\bin \(\s*"[^"]+"\s*,|\bin \[\s*"[^"]+"\s*,|\bin \{\s*"[^"]+"\s*,' "$SRC" 2>/dev/null \
  | grep -E "$BUSINESS" \
  | each LITERAL_SET_COMPARE "closed set of strings -> StrEnum in constants.py; compare against the enum"

# 8. Collaborators constructed in business code instead of injected.
grep -rn --include='*.py' -E '=\s*([A-Z][A-Za-z]*)?(Client|Gateway|Adapter|Repo|Repository|Service)\(|\.from_settings\(' "$SRC" 2>/dev/null \
  | grep -E "$BUSINESS" | grep -vE "$ROOT_FILES" \
  | each CONSTRUCTED_IN_PLACE "accept the collaborator in __init__; construct it only in dependencies.py or main.py"

# 9. A new HTTP connection pool per call instead of one shared client built in the lifespan.
grep -rn --include='*.py' -E 'httpx\.(Async)?Client\(|aiohttp\.ClientSession\(|requests\.(get|post|put|patch|delete)\(' "$SRC" 2>/dev/null \
  | grep -vE "$ROOT_FILES|/clients/" \
  | each HTTP_CLIENT_PER_CALL "build the HTTP client once in the lifespan; inject the client class"

# 10. Wall clock or random IDs read directly in business code (untestable without patching).
grep -rn --include='*.py' -E 'datetime\.(now|utcnow)\(|\buuid4\(\)|time\.time\(\)' "$SRC" 2>/dev/null \
  | grep -E "$BUSINESS" \
  | each CLOCK_NOT_INJECTED "inject Clock / IdFactory from utils/clock.py"

# 11. TYPE_CHECKING blocks outside models/ (type-only imports are plain imports; models/ may use it for ORM cycles).
grep -rn --include='*.py' -E '^if (typing\.)?TYPE_CHECKING:' "$SRC" 2>/dev/null \
  | grep -vE '/models?/' \
  | each TYPE_CHECKING_BLOCK "import at module top; TYPE_CHECKING only for ORM relationship cycles in models/"

# 12. Legacy SQLAlchemy declarations.
grep -rn --include='*.py' -E '\bColumn\(|declarative_base\(' "$SRC" 2>/dev/null \
  | grep -v mapped_column \
  | each LEGACY_SQLALCHEMY "use Mapped[...] = mapped_column(...) and DeclarativeBase"

cat "$OUT"
[ -s "$OUT" ] && exit 1
exit 0
