# Refactor recipes

Before → after for each recurring move. Each recipe names the test that pins it. Keep the observable behaviour identical unless the plan lists the change as a user decision. The target shapes are the files in `templates/`; copy from there rather than retyping.

## 1. `HTTPException` in a service → domain error + global handler

```python
# before (service)                                   # after (service)
if not order:                                        order = require(await self.orders.get(order_id), "Order")
    raise HTTPException(status_code=404,
                        detail="Order not found")    # after: app/exceptions.py + app/error_handlers.py from templates/
```
Map by the status the old code raised, message text byte-identical: 400→`BadRequestError`, 401→`UnauthorizedError`, 403→`ForbiddenError`, 404→`NotFoundError`, 409→`ConflictError`, 422→`UnprocessableError`, 502→`UpstreamError`. The status lives in `error_handlers.STATUS_BY_ERROR`, never on the exception class. Copy `templates/core/app/exceptions.py` and `templates/fastapi/app/error_handlers.py`; register once with `error_handlers.register(app)`. Tests that `pytest.raises(HTTPException)` against a service switch to the domain class and assert `.detail`.
**Pin:** `templates/fastapi/tests/unit/test_error_handlers.py` — a table test over a throwaway app with `ASGITransport(raise_app_exceptions=False)`: status, exact `{"detail": ...}`, no SQL text or `DETAIL:` in the body, 500 never echoes the exception.

## 2. Fat route → thin route + service method

```python
# before (route)                                     # after (route)
@router.get("/{item_id}")                            @router.get("/{item_id}", response_model=ItemResponse)
async def get_item(item_id, db=Depends(get_db)):     async def get_item(item_id: UUID, service: ItemServiceDep) -> ItemResponse:
    try:                                                 return await service.get_item(item_id)
        row = (await db.execute(select(Item)...))
    except Exception as e:                           # after (service)
        raise HTTPException(500, str(e))             async def get_item(self, item_id: UUID) -> ItemResponse:
    return {"id": row.id, "name": row.name}              item = require(await self.items.get(item_id), "Item")
                                                         return ItemResponse.model_validate(item, from_attributes=True)
```
Rules: one service call per endpoint (two calls = write the missing service method); queries, commits and dict literals leave the route; every endpoint has `response_model`. The DTO's JSON must equal the old dict's keys exactly — compare them in the task report.
**Pin:** API test asserting the response JSON before and after (same keys, same values).

## 3. Dataclass value type → `StrictModel` in `schemas/`

```python
# before                                # after (app/schemas/<domain>.py)
@dataclass                              class LineItem(StrictModel):
class LineItem:                             sku: str
    sku: str                                quantity: int
    quantity: int
```
Positional construction `LineItem("a1", 2)` becomes keyword `LineItem(sku="a1", quantity=2)` at every site (grep for `Name(` followed by a quote or digit). Upstream payload schemas use `extra="ignore"`; ORM-holding fields are `Any` so `schemas/` never imports `models/`. A dataclass that holds handles (session, client) becomes a plain class with a keyword-only `__init__`, not a pydantic model.
**Pin:** `ValidationError` on an extra field, on assignment, on a bad enum value.

## 4. Loose client functions + module-global transport → client class + port

```python
# before                                    # after (app/clients/<service>.py)
_client = None                              class Notifier(Protocol):
async def send_email(to, body, api_key):        async def send(self, *, to: str, body: str) -> MessageId: ...
    global _client
    _client = _client or httpx.AsyncClient()    class EmailClient:
    ...                                             def __init__(self, *, http: httpx.AsyncClient, retry: RetryPolicy) -> None: ...
                                                    @staticmethod
                                                    def build_http(settings: Settings) -> httpx.AsyncClient: ...
                                                    async def send(self, *, to: str, body: str) -> MessageId: ...
```
Only the operations callers perform become methods. The pooled `httpx.AsyncClient` is built by `build_http` inside `bootstrap.open_resources` and closed by its `AsyncExitStack`. Retry through `retry_async(..., should_retry=is_retryable)` with a module-level `is_retryable` that returns `True` only for 429/502/503/504 and connect/read timeouts. Add the fake to `tests/fakes/`. Grep tests for the old seam names (`monkeypatch.setattr(<module>, "_client"`) before deleting the old module — they fail at patch time, not import time. Shape: `templates/example-slice/app/clients/payments.py`.
**Pin:** `MockTransport` tests — transient status retried up to `max_attempts`; each fatal status makes exactly one call; a retried POST sends the same idempotency key every attempt.

## 5. Nested `async def` + `nonlocal` under `gather` → `bounded_gather`

```python
# before                                         # after
sem = asyncio.Semaphore(n); results = {}         async def score(self, item: Item) -> Score: ...
async def run(item):                             scores = await bounded_gather(items, self.score, limit=n)
    nonlocal total                               total = sum(s.value for s in scores)       # folded AFTER the gather
    async with sem: ...
    results[item.id] = ...
await asyncio.gather(*(run(i) for i in items))
```
`bounded_gather` (`templates/core/app/utils/concurrency.py`) uses a `TaskGroup`: a failure cancels the siblings and raises `ExceptionGroup`. The old `gather` let siblings run on after a failure — if callers depend on that, list it as a user decision. Callers that caught one exception type now use `except*`. Preserve every log substring existing tests assert.
**Pin:** `templates/core/tests/unit/test_concurrency.py` — order kept under reversed completion; peak in-flight == limit; empty input → `[]`; failure → `ExceptionGroup`; `limit=0` → `ValueError`.

## 6. `Path(__file__)` prompt loading → `load_prompt`

```python
# before                                                        # after (app/prompts/__init__.py)
PROMPTS = Path(__file__).parent.parent / "prompts"              @functools.cache
def get_prompt(name): return (PROMPTS / name).read_text()       def load_prompt(name: str) -> str:
                                                                    return (PROMPTS_DIR / f"{name}.md").read_text()
```
The native `FileNotFoundError` carries the path; no `is_file()` pre-check (it masks `PermissionError` as not-found). Inline instruction strings become `.md` files with `{placeholder}` and `.format(...)` at the one call site.
**Pin:** a known file loads byte-equal to `read_text()`; a missing name raises `FileNotFoundError` that names the file.

## 7. Blind `except Exception` fallback → classified

```python
# before                                    # after
except Exception as e:                      except Exception as exc:
    logger.warning("failed: %r", e)             if not is_retryable(exc):
    return fallback()                               raise
                                                logger.warning("%s failed (%s)", item.id, type(exc).__name__, exc_info=True)
                                                return Outcome(item_id=item.id, failed=True)
```
Fatal = cannot succeed on retry (auth, validation, bad URL scheme, illegal header, 3xx/405). Never `%r` a transport exception (header values are in it). Whether "every item failed" should fail the run is a **user decision**, listed in the plan, not decided in code.
**Pin:** each fatal class propagates with no fallback; each transient class yields the fallback and its type name appears in a WARNING.

## 8. Hard-coded `/api/` → config-driven version

`settings.api_version: int = 1`; `api_prefix` property → `/api/v{n}`; `routes.build_api_router(settings.api_prefix)` includes every domain router (each declares `/orders`-style prefixes); `/health` stays on the app. Tests: `sed 's#/api/#/api/v1/#g'` over test dirs after a safety grep that every `/api/` is one of ours; also the frontend `baseURL`, raw `fetch` URLs, `scripts/` and seed files (grep the whole repo).
**Pin:** routes under the prefix, `/health` not; `GET` on an old path → 404.

## 9. Dead code

Delete only with evidence: `grep -rn "<module>\b" app tests scripts` shows no runtime importer (tests-only importers mean the module AND its tests go). Before deleting a module that reads a data file (prompts, templates), confirm nothing else reads that directory. Commit the sweep alone so it is bisectable.

## 10. Repeated `if not x: raise NotFoundError` → `require()`

```python
# before (service, ×N)                              # after
order = await self.orders.get(order_id)             order = require(await self.orders.get(order_id), "Order")
if not order:
    raise NotFoundError("Order not found")
```
`require` tests `is None`, not falsiness (an empty list is a value). The message becomes `f"{what} not found"` — list every site whose old message differs as a **decision for the user** (default: adopt the uniform message). A guard that raises a different error gets its own helper (`require_owner(obj, user_id)` → `ForbiddenError`), never a `status` parameter.
**Pin:** `templates/core/tests/unit/test_exceptions.py`.

## 11. Collaborator built in place → constructor parameter + provider

```python
# before (service)                                  # after (service)
async def build_report(db, report_id):              class ReportService:
    exporter = PdfExporter()                            def __init__(self, *, session: AsyncSession, reports: ReportRepo,
    now = datetime.now()                                             exporter: Exporter, clock: Clock) -> None: ...
    ...
# before (test)                                     # after (app/dependencies.py)
monkeypatch.setattr(mod, "PdfExporter", Fake)       def get_report_service(session: SessionDep, resources: ResourcesDep) -> ReportService:
                                                        return ReportService(session=session, reports=ReportRepo(session),
                                                                             exporter=resources.exporter, clock=utc_now)
                                                    # after (test)
                                                    ReportService(session=session, reports=ReportRepo(session),
                                                                  exporter=FakeExporter(), clock=lambda: NOW)
```
Long-lived collaborators (HTTP clients, exporters with pools) go on `Resources` in `bootstrap.py`; per-request ones (repos, services) are built in the provider. Scripts build the same graph from `open_resources`. Grep tests for `monkeypatch.setattr(<module>, "<Class>"` — each is a seam that moves to the parameter.
**Pin:** the service test passes fakes through the constructor and never imports `monkeypatch`; the API test uses `app.dependency_overrides` and asserts the fake was called.

## 12. Literal status strings → `StrEnum` + `match`

```python
# before                                            # after (app/constants.py)
if order.status == "paid": ...                      class OrderStatus(StrEnum):
elif order.status in ("pending", "draft"): ...          PAID = "paid"; PENDING = "pending"; DRAFT = "draft"
                                                    # after (service)
                                                    match order.status:
                                                        case OrderStatus.PAID: ...
                                                        case OrderStatus.PENDING | OrderStatus.DRAFT: ...
                                                        case unreachable: assert_never(unreachable)
```
`StrEnum` keeps `== "paid"` true and serialises to the string, so the wire format is unchanged. A schema field `status: Literal["paid", ...]` becomes `status: OrderStatus`. For the DB column, check what is stored: `Enum(OrderStatus, native_enum=False)` stores member **names**; if the existing rows hold values, use `values_callable=lambda e: [m.value for m in e]` or keep a `String` column. Magic numbers become `Final` constants in `constants.py`.
**Pin:** `OrderStatus("paid") is OrderStatus.PAID`; a response round-trips to the same JSON string; existing rows load into the enum.
