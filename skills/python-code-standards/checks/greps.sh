#!/usr/bin/env bash
# Repo-specific pattern checks that no linter expresses. Usage: greps.sh <src_dir> [routers_dir]
# Prints one finding per line as  path:line: RULE message  and exits 1 if any were found.
set -u
SRC="${1:?src dir}"
ROUTERS="${2:-}"
found=0
emit() { printf '%s\n' "$1"; found=1; }

# 1. HTTPException may be imported by exactly one module (the global handler). Report when >1.
importers=$(grep -rln --include='*.py' -E '^\s*from fastapi(\.[a-z_.]+)? import .*\bHTTPException\b|^\s*import fastapi' "$SRC" 2>/dev/null \
  | xargs -I{} grep -l 'HTTPException' {} 2>/dev/null | sort -u)
n=$(printf '%s\n' "$importers" | sed '/^$/d' | wc -l | tr -d ' ')
if [ "$n" -gt 1 ]; then
  while IFS= read -r f; do [ -n "$f" ] && emit "$f:1: HTTPEXCEPTION_IMPORTERS $n modules import HTTPException; only the error-handler module may"; done <<<"$importers"
fi

# 2. Prompt loading by path arithmetic outside a prompts package.
grep -rn --include='*.py' -E 'Path\(__file__\).*(prompt|playbook)|read_text\(\)' "$SRC" 2>/dev/null \
  | grep -vE '/prompts/__init__\.py:|/prompts\.py:' \
  | while IFS= read -r l; do emit "${l%%:*}:$(echo "$l" | cut -d: -f2): PROMPT_PATH_HACK load prompts via the prompts package's load_prompt()"; done

# 3. Nested async closure + nonlocal (shared mutable state under gather).
grep -rn --include='*.py' -E '^\s{8,}async def |^\s+nonlocal ' "$SRC" 2>/dev/null \
  | while IFS= read -r l; do emit "${l%%:*}:$(echo "$l" | cut -d: -f2): NESTED_ASYNC_CLOSURE extract to utils/concurrency.bounded_gather + a method"; done

# 4. Module-global transport seam (monkeypatch hook) instead of constructor injection.
grep -rn --include='*.py' -E '^_transport\b|^_client\b|^_session\b' "$SRC" 2>/dev/null \
  | while IFS= read -r l; do emit "${l%%:*}:$(echo "$l" | cut -d: -f2): MODULE_GLOBAL_SEAM inject via a client class constructor (transport=...)"; done

# 5. Private helper sprawl: >= 3 leading-underscore defs in one module (dunders excluded).
for f in $(grep -rl --include='*.py' -E '^\s*(async )?def _[a-z]' "$SRC" 2>/dev/null); do
  c=$(grep -cE '^\s*(async )?def _[a-z]' "$f")
  [ "$c" -ge 3 ] && emit "$f:1: PRIVATE_HELPER_SPRAWL $c leading-underscore defs; name and test them, or inline"
done

# 6. Routers doing non-controller work.
if [ -n "$ROUTERS" ] && [ -d "$ROUTERS" ]; then
  grep -rn --include='*.py' -E '^\s+return \{|\.commit\(\)|select\(|\.execute\(' "$ROUTERS" 2>/dev/null \
    | while IFS= read -r l; do emit "${l%%:*}:$(echo "$l" | cut -d: -f2): FAT_ROUTER controllers call one service and return a DTO"; done
  grep -rn --include='*.py' -E 'prefix="/api/' "$ROUTERS" 2>/dev/null \
    | while IFS= read -r l; do emit "${l%%:*}:$(echo "$l" | cut -d: -f2): HARDCODED_API_PREFIX mount under settings.api_prefix; routers declare domain-relative prefixes"; done
fi

# 7. Transaction ownership: the use-case owner commits once; repos flush, routers never touch the session.
#    Owners default to service modules and orchestration engines; override with PCS_COMMIT_OWNERS (ERE path fragment).
OWNERS="${PCS_COMMIT_OWNERS:-/services?/|/engine\.py:|/orchestrat[a-z]*\.py:}"
grep -rn --include='*.py' -E '\.commit\(\)' "$SRC" 2>/dev/null \
  | grep -vE "$OWNERS|/database\.py:|/db\.py:|/session\.py:|/tests?/" \
  | while IFS= read -r l; do emit "${l%%:*}:$(echo "$l" | cut -d: -f2): COMMIT_OUTSIDE_SERVICE the use-case service commits once at its end; repos flush, routers never commit"; done

# 8. Literal string sets in business code: `x in ("a", "b")` is a missing StrEnum (ruff PLR2004 covers `==`).
grep -rn --include='*.py' -E '\bin \(\s*"[^"]+"\s*,|\bin \[\s*"[^"]+"\s*,|\bin \{\s*"[^"]+"\s*,' "$SRC" 2>/dev/null \
  | grep -E '/(services?|routers?|controllers?|harness|domain)/' \
  | while IFS= read -r l; do emit "${l%%:*}:$(echo "$l" | cut -d: -f2): LITERAL_SET_COMPARE closed set of strings -> StrEnum in constants/; compare against the enum"; done

# 9. Collaborators constructed in place instead of injected (Spring-style DI): a service module calling
#     SomeClient(...) / Matcher() / .from_settings(...) builds its own dependency. Providers live in dependencies.py.
grep -rn --include='*.py' -E '=\s*([A-Z][A-Za-z]*)?(Client|Matcher|Gateway|Adapter|Repository)\(|\.from_settings\(' "$SRC" 2>/dev/null \
  | grep -E '/(services?|routers?|controllers?)/' \
  | grep -vE '/dependencies\.py:|/deps\.py:|/providers\.py:' \
  | while IFS= read -r l; do emit "${l%%:*}:$(echo "$l" | cut -d: -f2): CONSTRUCTED_IN_PLACE accept the collaborator as a parameter / Depends(provider); construct only in dependencies.py"; done

# 11. TYPE_CHECKING blocks outside models/ (type-only imports are plain imports; models/ may use it for ORM cycles).
grep -rn --include='*.py' -E '^if (typing\.)?TYPE_CHECKING:' "$SRC" 2>/dev/null \
  | grep -vE '/models?/' \
  | while IFS= read -r l; do emit "${l%%:*}:$(echo "$l" | cut -d: -f2): TYPE_CHECKING_BLOCK import at module top; TYPE_CHECKING only for ORM relationship cycles in models/"; done

# 10. Legacy SQLAlchemy declarations.
grep -rn --include='*.py' -E '\bColumn\(|declarative_base\(' "$SRC" 2>/dev/null \
  | grep -v mapped_column \
  | while IFS= read -r l; do emit "${l%%:*}:$(echo "$l" | cut -d: -f2): LEGACY_SQLALCHEMY use Mapped[...] = mapped_column(...) and DeclarativeBase"; done

exit $found
