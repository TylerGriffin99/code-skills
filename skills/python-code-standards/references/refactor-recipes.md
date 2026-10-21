# Refactor recipes

Before → after for each recurring move. Each recipe names the test that pins it. Keep the observable behaviour identical unless the plan lists the change as a user decision.

## 1. `HTTPException` in a service → domain error + global handler

```python
# before (service)
if not claim:
    raise HTTPException(status_code=404, detail="Claim not found")

# after (service)                         # after (exceptions.py)
if not claim:                             class AppError(Exception):
    raise NotFoundError("Claim not found")    status_code: int = 500
                                              headers: ClassVar[dict[str, str] | None] = None
                                              def __init__(self, detail: str) -> None:
                                                  super().__init__(detail); self.detail = detail
                                          class NotFoundError(AppError): status_code = 404
```
Mapping by status, message text byte-identical: 400→`BadRequestError`, 401→`UnauthorizedError` (headers `{"WWW-Authenticate": "Bearer"}`), 403→`ForbiddenError`, 404→`NotFoundError`, 409→`ConflictError`, 422→`UnprocessableError`. Register handlers once (`error_handlers.register(app)`): `AppError`, `IntegrityError` (SQLSTATE 23505/23503→409 else 400), `DataError`→400, `NoResultFound`→404, `OperationalError`/`InterfaceError`/`TimeoutError`→503, `httpx.HTTPStatusError`→502, `Exception`→500 `"Internal server error"`. Handlers log type + SQLSTATE; `driver_message` = first line of `str(exc.orig)`, class name if empty. Tests that `pytest.raises(HTTPException)` against a service switch to the domain class; `.status_code` still works (class attr).
**Pin:** a table test over a throwaway `FastAPI()` app with one route per handler row, `ASGITransport(raise_app_exceptions=False)`; asserts status, exact `{"detail": ...}`, that SQL text/params/`DETAIL:` never appear, that 500 never echoes the exception.

## 2. Fat router → thin router + DTO classmethod

```python
# before (router)                                   # after (router)
def _build_item_response(summary) -> dict: ...      @router.get("/{item_id}", response_model=ItemResponse)
@router.get("/{item_id}")                            async def get_item(item_id: uuid.UUID, db=Depends(get_db)):
async def get_item(item_id, db=Depends(get_db)):         return await item_service.get_item(db, item_id)
    try: summary = await item_service.get_item(...)
    except HTTPException: raise                     # after (schemas/item.py)
    except Exception as e:                          class ItemResponse(BaseModel):
        raise HTTPException(500, str(e))                ...
    return _build_item_response(summary)                @classmethod
                                                        def from_summary(cls, s: ItemSummary) -> ItemResponse: ...
```
Rules: one service call per endpoint (two calls = write the missing service function); queries, commits and dict literals leave the router; every endpoint has `response_model`; the service returns the DTO or the router wraps with `Dto.from_*`. The DTO's JSON must equal the old dict's keys exactly — compare them in the task report.
**Pin:** API test asserting the response JSON before and after (same keys, same values); unit test of `from_*` with a `SimpleNamespace` stand-in.

## 3. Dataclass value type → pydantic `StrictModel` in `schemas/`

```python
# before (utils/types.py)             # after (schemas/base.py + schemas/<domain>.py)
@dataclass                            class StrictModel(BaseModel):
class Subcat:                             model_config = ConfigDict(extra="forbid", frozen=True)
    id: str                           class Subcat(StrictModel):
    code: str                             id: str
                                          code: str
```
Positional construction `Subcat("u1", "DM")` becomes keyword `Subcat(id="u1", code="DM")` at every site (grep for `Name(` with a quote right after). Keep LLM-output schemas `extra="ignore"`; keep ORM-holding fields `Any` so `schemas/` never imports `models/`. Frozen is shallow — fine. Dataclasses stay only in the exempt subtree the plan names.
**Pin:** `ValidationError` on an extra field, on assignment, on a bad `Literal`.

## 4. Loose client functions + module-global transport → client class

```python
# before                                            # after (clients/<upstream>_client.py)
_transport = None                                   class JevClient:
async def call(*, state, url, api_key): ...             def __init__(self, *, url, api_key, model, timeout=30.0,
async def call_with_retry(fn, ...): ...                              retry: RetryPolicy = DEFAULT_RETRY, transport=None): ...
                                                        @classmethod
                                                        def from_settings(cls, settings) -> JevClient: ...
                                                        async def choose(self, *, qid, state, criteria, instructions) -> Response: ...
                                                        @staticmethod
                                                        def is_fatal(exc) -> bool: ...   # config/auth/credit + transport misconfig
                                                        @staticmethod
                                                        def is_retryable(exc) -> bool: ... # 429/529 only
```
Only the operations the caller performs become methods (no speculative ones). Retry via `utils/retry.py::retry_async(fn, *, policy, should_retry)`. Tests inject `transport=httpx.MockTransport(handler)`; a legacy `decide_fn` double adapts through a `fake_client(decide)` helper so existing tests change one constructor line. Grep tests for the OLD seam names (`call_decisions`, `_transport`) before deleting the old module — they fail at monkeypatch time, not import time.
**Pin:** retry count on 529 equals `max_attempts`; fatal statuses make exactly one call; transport misconfig (`UnsupportedProtocol`, `LocalProtocolError`, `InvalidURL`, 3xx/405) is fatal; `ConnectError` is not.

## 5. Nested `async def run` + `nonlocal` under `gather` → `bounded_gather` + method

```python
# before                                         # after
sem = asyncio.Semaphore(n); results = {}         async def decide_item(self, item, *, criteria) -> ItemDecision: ...
async def run(item):                             decisions = await bounded_gather(items,
    nonlocal tok                                     functools.partial(self.decide_item, criteria=criteria), limit=n)
    async with sem: ...                           results = {d.item_index: ... for d in decisions}   # folded AFTER gather
    results[item.id] = ...
await asyncio.gather(*(run(i) for i in items))
```
`bounded_gather(items, fn, *, limit)` owns the semaphore, returns results in input order, propagates the first exception (siblings are not cancelled — say so in the docstring), raises `ValueError` for `limit < 1`. The per-item function returns an immutable value; totals are summed after the gather. Preserve every log substring existing tests assert.
**Pin:** order preserved under reversed completion; max in-flight == limit; empty input → `[]`; exception propagates; `limit=0` → `ValueError`.

## 6. `Path(__file__)` prompt loading → `load_prompt`

```python
# before                                                        # after (prompts/__init__.py)
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"         @functools.cache
def _load_prompt(name): return (_PROMPTS_DIR / name).read_text() def load_prompt(name: str) -> str:
                                                                    return (PROMPTS_DIR / f"{name}.md").read_text()
```
Native `FileNotFoundError` carries the path; no `is_file()` pre-check (it masks `PermissionError` as not-found on 3.14). Inline instruction strings become `.md` files with `{placeholder}` and `.strip().format(...)` at the one call site.
**Pin:** loads a known file byte-equal to `read_text()`; missing name raises `FileNotFoundError` matching the filename.

## 7. Blind `except Exception` fallback → classified

```python
# before                                    # after
except Exception as e:                      except Exception as exc:
    logger.warning("failed: %r", e)             if self.client.is_fatal(exc):
    results[i] = fallback()                         raise
                                                logger.warning("... failed (%s)", type(exc).__name__, exc_info=True)
                                                return ItemDecision(item_index=i, failed=True)
```
Fatal = cannot succeed on retry (auth/credit/config statuses, bad URL scheme, illegal header value, 3xx/405). Never `%r` a transport exception (the header value is in it). Whether "all items failed" should fail the run is a **user decision**, listed in the plan, not decided in code.
**Pin:** each fatal class raises out of `match()` with no fallback call; each transient class yields the fallback and the type name appears in a WARNING.

## 8. Hard-coded `/api/` → config-driven version

`settings.api_version: int = 1`; `@property api_prefix -> f"/api/v{self.api_version}"`; one `APIRouter(prefix=settings.api_prefix)` includes every domain router (which declare `/projects/...`-style prefixes); `/health` stays on `app`. Tests: `sed 's#/api/#/api/v1/#g'` over test dirs after a safety grep that every `/api/` is one of ours; frontend `baseURL` and raw `fetch`/`EventSource` URLs; **scripts/** and seed files too (grep the whole repo, not just tests).
**Pin:** prefix derived from version; routes under the prefix and `/health` not; `GET /api/<old>` → 404.

## 9. Hand-written assignment constructor → `@dataclass`

Six `self.x = x` lines → `@dataclass` fields; `cancel_event: asyncio.Event = field(default_factory=asyncio.Event)` (never a shared default instance). Behaviour objects holding sessions/handles stay dataclasses, not pydantic. Check every construction site uses keywords before relying on field order.

## 10. Dead code

Delete only with evidence: `grep -rn "<module>\b" src tests` shows no runtime importer (tests-only importers mean the module AND its tests go). Before deleting a module that reads a data file (prompts, templates), confirm nothing else reads that directory. Commit the sweep alone so it is bisectable.

## 11. Repeated `if not x: raise NotFoundError` → `require()`

```python
# before (service, ×N)                              # after (exceptions.py)
claim = await claim_repo.get_by_id(db, claim_id)    def require[T](value: T | None, what: str) -> T:
if not claim:                                           if value is None:
    raise NotFoundError("Claim not found")                  raise NotFoundError(f"{what} not found")
                                                        return value
                                                    # after (service)
                                                    claim = require(await claim_repo.get_by_id(db, claim_id), "Claim")
```
`is None`, not falsiness (an empty list is a value). Message text changes from the hand-written string to `f"{what} not found"` — list every site whose old message differs as a **decision for the user** (default: adopt the uniform message). A guard raising a different status gets its own helper (`require_owned(obj, user_id)` → `ForbiddenError`), never a `status` parameter.
**Pin:** `require(None, "Claim")` raises `NotFoundError` with detail `Claim not found`; `require(obj, ...)` returns `obj` unchanged (identity).

## 12. Collaborator built in place → provider + injected parameter

```python
# before (service)                                  # after (dependencies.py)
async def build_report(db, item_id):                def get_matcher(settings: Settings = Depends(get_settings)) -> Matcher:
    matcher = Matcher()                                 return Matcher(client=JevClient.from_settings(settings))
    ...                                             # after (service)
                                                    async def build_report(db, item_id, *, matcher: Matcher): ...
# before (test)                                     # after (router)
monkeypatch.setattr(mod, "Matcher", FakeMatcher)    async def report(..., matcher: Matcher = Depends(get_matcher)):
                                                        return await report_service.build_report(db, item_id, matcher=matcher)
                                                    # after (test)
                                                    app.dependency_overrides[get_matcher] = lambda: FakeMatcher()
```
Non-request code (an engine, a CLI) gets the same shape without FastAPI: a `build_<thing>(settings)` factory in one module, and every function below it takes the collaborator as a keyword-only parameter. A class whose only state is one injected collaborator becomes functions taking that collaborator. Grep tests for `monkeypatch.setattr(<module>, "<Class>"` — each is a seam that moves to the parameter.
**Pin:** the service test passes a fake through the parameter and never imports `monkeypatch`; the router test uses `dependency_overrides` and asserts the fake was called.

## 13. Literal status strings → `StrEnum` + derived `Literal` + `match`

```python
# before                                            # after (constants/enums.py)
if item.status == "active": ...                     class ItemStatus(StrEnum):
elif item.status in ("pending", "draft"): ...           ACTIVE = "active"; ARCHIVED = "archived"
                                                        PENDING = "pending"; DRAFT = "draft"
# before (schema)                                   # after (schema)
status: Literal["active", "archived", ...]          status: ItemStatus          # pydantic validates by value
# after (service)
match item.status:
    case ItemStatus.ACTIVE: ...
    case ItemStatus.PENDING | ItemStatus.DRAFT: ...
```
`StrEnum` keeps `== "active"` true and JSON-serialises to the string, so wire format and DB values are unchanged. Constants that are not sets (`MAX_CONTEXT_TOKENS: Final = 8_000`) go in `constants/__init__.py` (or `constants.py`); the magic number in the comparison becomes the name. Where a `Literal` must stay (pydantic discriminators), derive it: `ItemStatusLiteral = Literal[*ItemStatus]` is not valid syntax — write `Literal["active", "archived"]` once next to the enum with a test asserting `set(get_args(ItemStatusLiteral)) == {s.value for s in ItemStatus}`.
**Pin:** `ItemStatus("active") is ItemStatus.ACTIVE`; a response model round-trips the enum to the same JSON string; the `match` has no `case _:` that swallows a new member (assert exhaustiveness with a type-checker `assert_never`).
