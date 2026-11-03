# Standards

The single source of truth for how a Python project is built. Build mode follows it from the first file; refactor mode measures an existing repo against it. Each rule has its reason and its enforcement form. **Mechanical** rules are enforced by `checks/` through `scripts/check.sh` — spend no review attention on them. **Judgement** rules are what the author and the reviewer look for.

Two tiers: **core** applies to any Python project; the **FastAPI profile** applies when the project serves HTTP (`detect.sh` reports `fastapi: yes`).

## Project structure

```
<repo>/
  app/
    routes/          # FastAPI profile only: thin controllers, one service call each
    services/        # use cases; own the transaction; take collaborators by constructor
    repos/           # data access; return models; flush, never commit
    schemas/         # every pydantic type: StrictModel value types, request/response DTOs, upstream payloads
    models/          # SQLAlchemy ORM models (2.0 style); __init__.py imports every model
    database/        # Base, engine, session factory, session_scope, custom column types
    clients/         # one module per external service: Protocol port + real client
    utils/           # framework-free helpers: retry, bounded concurrency, clock
    config.py        # Settings (pydantic-settings) + get_settings()
    constants.py     # StrEnums for closed sets, Final constants
    exceptions.py    # domain errors (no HTTP knowledge) + require()
    bootstrap.py     # composition root: open_resources(settings) builds every long-lived resource
    dependencies.py  # FastAPI profile: per-request providers built from Resources
    error_handlers.py# FastAPI profile: domain/driver error -> status code + {"detail": ...}
    main.py          # FastAPI profile: create_app(settings), lifespan calls open_resources
  scripts/           # CLI entry points; each opens resources through bootstrap.py
  tests/
    unit/            # services against a real test DB + fakes; clients against httpx.MockTransport
    api/             # FastAPI profile: through the ASGI app, fakes via dependency_overrides
    fakes/           # one in-memory implementation per Protocol port
```

A layer with nothing in it yet keeps its empty package, so the layout reads the same in every project. `templates/core` and `templates/fastapi` are this tree, tested (`scripts/verify-templates.sh`).

## Core (any Python)

### Dependencies and composition

| Rule | Why | Form |
|---|---|---|
| **Accept collaborators, never construct them.** A service takes its repos, clients, clock and ID factory as keyword-only `__init__` parameters. Only `bootstrap.py`, `dependencies.py` and `main.py` call constructors. | Inversion of control: the test seam is the parameter, so a fake is one argument and no `monkeypatch` is needed. | mechanical: `greps.sh` `CONSTRUCTED_IN_PLACE`; judgement: provider shape |
| **One composition root.** `bootstrap.open_resources(settings)` is an async context manager that builds the engine, session factory and every external client once, yields a `Resources` holder, and closes them in reverse order (`AsyncExitStack`). Scripts and the FastAPI lifespan both call it. | One place to read the whole object graph; nothing leaks a connection pool. | judgement |
| **No DI container library.** Plain constructor injection plus the composition root. | A container hides the wiring a reader wants to see, and must be defended. | judgement |
| **Shared clients.** Each external service gets one pooled `httpx.AsyncClient` (base URL, auth headers, timeout) built in `open_resources` and closed at shutdown. The client class receives it as `http=`. Never a client per call or per request. | A pool per call loses keep-alive, leaks sockets, and scatters auth and timeout config. | mechanical: `greps.sh` `HTTP_CLIENT_PER_CALL`, `MODULE_GLOBAL_SEAM` |
| **Time and identity are injected.** Services take `clock: Clock` and `new_id: IdFactory` (`utils/clock.py`); datetimes are timezone-aware UTC. | Deterministic tests with no patching; no naive-datetime bugs. | mechanical: ruff `DTZ`, `greps.sh` `CLOCK_NOT_INJECTED` |
| **Settings** are one `pydantic-settings` `BaseSettings` in `config.py`, `frozen=True`; secrets are `SecretStr`; connection URLs and credentials have no default, so a missing one fails at startup instead of pointing at a local file or a placeholder host. `get_settings()` is `@lru_cache`d and called only by entry points. | Config is validated once; a missing key fails fast instead of degrading later. | judgement |

### Interfaces

| Rule | Why | Form |
|---|---|---|
| **A `Protocol` port at each external boundary** (a third-party API, a message bus, a file store). The port lives beside its real client in `clients/<service>.py`; the in-memory fake lives in `tests/fakes/`. Consumers type their parameter as the port. | The fake is the second implementation that makes the seam real; structural typing means the fake needs no inheritance and mypy checks it fits. | judgement |
| **`abc.ABC` only for shared implementation** (a template method that subclasses fill in) or when instantiating an incomplete subclass must fail. | An ABC with only abstract methods is a Protocol with extra coupling. | judgement |
| **No interface between pure functions or in front of a repo with one store.** Repos are concrete classes; service tests run them against a real test database (SQLite file per test). Add a repo Protocol only when a second store exists. | Speculative interfaces cost the reader and read as poor judgement; a real DB catches query bugs a fake hides. | judgement |
| **Deep modules, tight seams.** Few public names per module; `__all__` on packages that re-export. Repos expose domain verbs (`get_open_orders`), not query builders. **Deletion test:** if removing a module makes complexity vanish, it was a pass-through. | Depth = behaviour per unit of interface learned. | judgement (vocabulary: `mattpocock-skills:codebase-design`) |

### Types and schemas

| Rule | Why | Form |
|---|---|---|
| **Strict typing.** Every function is annotated; `mypy --strict` passes. | The type checker proves the wiring; this is the strongest single quality signal. | mechanical: ruff `ANN`, `check.sh` mypy with `checks/mypy.ini` |
| Value types are pydantic models in `schemas/`; internal ones extend `StrictModel` (`extra="forbid"`, `frozen=True`). Upstream payloads use `extra="ignore"` so a provider addition does not break parsing. | Validation at construction; one place for every shape. | mechanical: ruff `TID251` bans `dataclasses.dataclass`; judgement: which models stay lax |
| Objects that hold handles (sessions, HTTP clients, `Resources`) are plain classes with a keyword-only `__init__` — never pydantic, never dataclasses. | Pydantic would validate or copy a live handle; one rule for every holder. | judgement |
| `schemas/` never imports `models/`. ORM → DTO is `Dto.model_validate(row, from_attributes=True)` in the service. A `from_*` classmethod exists only when it does real mapping work. | Schemas stay a leaf; no shallow copy methods. | mechanical: import-linter `leaf-packages` |
| **Scalar constraints are `Annotated` aliases** (`Cents = Annotated[int, Field(gt=0)]`) in `schemas/common.py`, reused by every model that carries the value. | One place for the rule; the field name reads as a type. | judgement |
| **Closed sets are `StrEnum`s, constants are `Final`, both in `constants.py`.** Business code compares against the enum member and dispatches with `match` + `assert_never`. | One place to spell `"paid"`; exhaustiveness is checked. | mechanical: ruff `PLR2004`, `greps.sh` `LITERAL_SET_COMPARE` |
| No `if TYPE_CHECKING:` blocks except in `models/` for a genuine ORM relationship cycle. | A type-only import is a plain import; a `TYPE_CHECKING` import that dodges a layer rule hides a real violation. | mechanical: `greps.sh` `TYPE_CHECKING_BLOCK` |

### Errors

| Rule | Why | Form |
|---|---|---|
| Domain errors live in `exceptions.py`: `AppError(detail)` and subclasses (`NotFoundError`, `ConflictError`, `UpstreamError`, …). They carry **no HTTP status**; `error_handlers.py` maps type → status. | The domain does not know it is served over HTTP; scripts reuse it unchanged. | mechanical: ruff `TID251` bans `fastapi.HTTPException`; import-linter `http-free` |
| **`require(value, what)`** replaces `x = await repo.get(...); if x is None: raise NotFoundError(...)`. It tests `is None`, not falsiness. | One line per guard, uniform messages. | judgement |
| **Error paths are classified.** Fatal (cannot succeed on retry: 4xx auth/validation, bad URL, misconfiguration) propagates on the first attempt. Transient (429, 502/503/504, connect/read timeouts) is retried with `retry_async`, then propagates. An `except Exception` that turns both into one fallback is a defect. | A bad API key must fail the run, not quietly degrade every item. | mechanical: ruff `BLE001`; judgement; `silent-failure-hunter` verifies |
| **A retried non-idempotent call carries an idempotency key** (`Idempotency-Key` header or request reference). | A retried POST without one can charge or create twice. | judgement |
| Log the exception type and context, never `%r` of a transport exception (it can carry header values). | Secrets stay out of logs. | judgement |

### Functions and classes

| Rule | Why | Form |
|---|---|---|
| **Pure functions for logic, classes for held collaborators.** A service or repo is a class because it holds injected collaborators; logic that needs none is a module-level function. Never mutate a parameter; build results after a gather, not inside it. | Fewer moving parts to test; the type checker proves more. | mechanical: ruff `RET`, `PERF401/402`, `SIM`; judgement |
| **Conciseness ceilings:** complexity ≤ 10, args ≤ 6, branches ≤ 10, statements ≤ 40, returns ≤ 6. Over the ceiling means a missing value object, a missing function, or a dispatch table. Booleans are keyword-only. | Code grows by appending; ceilings force it to split. | mechanical: ruff `C901`, `PLR0911/0912/0913/0915`, `FBT` |
| Retry and bounded fan-out are utilities: `utils/retry.py::retry_async`, `utils/concurrency.py::bounded_gather` (a `TaskGroup` + semaphore: a failure cancels the siblings and raises `ExceptionGroup`). No nested `async def` + `nonlocal` closures. | Shared state mutated under `gather` is the classic race; a utility is tested once. | mechanical: `greps.sh` `NESTED_ASYNC_CLOSURE` |
| Imports at module top. | Lazy imports hide dependencies and cycles; fix cycles by restructuring. | mechanical: ruff `PLC0415` |
| No leading-underscore helper sprawl: a helper worth writing is worth a public name and a test. | Private names signal "don't test me" and pile up into untestable blobs. | mechanical: `greps.sh` `PRIVATE_HELPER_SPRAWL` |
| Dead code is deleted with importer evidence (`grep -rn "<module>" app tests scripts` shows no runtime caller). | A deleted file cannot rot. | judgement |
| Prompts, when the project has them, are `.md` files under `app/prompts/` loaded by one `load_prompt(name)` (`functools.cache`). | One way to find every prompt. | mechanical: `greps.sh` `PROMPT_PATH_HACK` |

### Data

| Rule | Why | Form |
|---|---|---|
| **Transaction ownership:** the service owns every commit; repos `flush`; routes and scripts never touch the session directly. Commit before an external call and again after it, so no transaction stays open while a provider is slow and a successful charge never loses its row. `session_scope` (an async context manager) rolls back when the unit of work raises. | One owner per transaction; a repo that commits cannot be composed. | mechanical: `greps.sh` `COMMIT_OUTSIDE_SERVICE` |
| ORM models are SQLAlchemy 2.0 style: `DeclarativeBase`, `Mapped[...] = mapped_column(...)`, `__tablename__` on each. Datetime columns use `database/types.py::UtcDateTime`. | Typed columns; aware datetimes on every backend. | mechanical: `greps.sh` `LEGACY_SQLALCHEMY` |
| Layers: `services → repos → models → database`. `schemas`, `clients` and `utils` import no app layer. | Each layer is replaceable and testable alone. | mechanical: import-linter `layers`, `leaf-packages` |

### Tests

| Rule | Why | Form |
|---|---|---|
| Fakes for owned ports (`tests/fakes/`), never `MagicMock` of your own interfaces. External HTTP is faked at the transport: `httpx.MockTransport`. | A fake that drifts from the port fails mypy; a mock accepts anything. | judgement |
| One behaviour per test, named for it (`test_fatal_status_makes_exactly_one_call`). Arrange / act / assert separated by blank lines. Tables use `pytest.mark.parametrize`. | The test list reads as the specification. | mechanical: ruff `PT`; judgement |
| Every retry, fallback and error mapping has a test that pins it: retry count, fatal = one call, the status and exact `{"detail": ...}` body, no SQL in any error body. | These are the paths that fail silently in production. | judgement |

### Docs

| Rule | Why | Form |
|---|---|---|
| **Few docstrings, fewer comments.** A one-line docstring only where the name cannot carry it: an invariant, a unit, a failure mode. A comment says *why*. No commented-out code, no `print`. | Docstrings that paraphrase the signature are sediment. | mechanical: ruff `ERA001`, `T20` |
| The project `README.md` says how to run and test it, the design decisions with their trade-offs, and what is left out. | The reader judges the decisions, not only the code. | judgement |
| Line length 120, ruff-formatted, `skip-magic-trailing-comma = true`. | One formatter decision. | mechanical: ruff `E501` + `format --check` |

## FastAPI profile

| Rule | Why | Form |
|---|---|---|
| Layers `routes → services → repos → models → database`. Routes never import repos, models, database or `sqlalchemy`; they depend on a service provider. | Controllers take a request, call one service method, return a DTO. | mechanical: import-linter `layers`, `thin-routes` |
| Only `routes/`, `dependencies.py`, `main.py` and `error_handlers.py` import `fastapi`/`starlette`. | The domain is reusable from scripts and tests without a web app. | mechanical: import-linter `http-free` |
| A route makes one service call and returns its DTO; `response_model` on every endpoint; validation by typed params and request DTOs. Two calls in a route = a missing service method. | Thin controllers. | mechanical: `greps.sh` `FAT_ROUTE`; judgement |
| `main.create_app(settings)` builds the app; its `lifespan` calls `open_resources` and stores `Resources` on `app.state`. `dependencies.py` reads `Resources` and builds services and repos per request; `Annotated[..., Depends(...)]` aliases (`OrderServiceDep`). Tests swap a port with `app.dependency_overrides[get_payments]`. Entry point: `uvicorn --factory app.main:build_app`. | Resources built once per process, services once per request, no import-time side effects. | judgement |
| `error_handlers.register(app)` maps: domain errors by type (MRO walk, so subclasses inherit); `IntegrityError` by SQLSTATE (23505/23503 → 409, other or unknown → 400); `DataError` → 400; `NoResultFound` → 404; `OperationalError`/`InterfaceError`/`TimeoutError` → 503 with the fixed detail `Service unavailable` (their driver message names hosts and users); any `httpx.HTTPError` left after retries → 502 `Upstream service error`; anything else → 500 `Internal server error`. For integrity and data errors the detail is the first line of `str(exc.orig)`, never `str(exc)` (that carries the SQL and parameters). 5xx responses are logged at ERROR with the traceback. | FastAPI-native `{"detail": ...}` bodies; nothing leaks the query; an outage is a 503. | judgement + table test (`tests/unit/test_error_handlers.py`) |
| API version comes from config: `settings.api_prefix` (`/api/v1`); domain routers declare relative prefixes (`/orders`) and mount under one versioned router; `/health` stays unversioned. | A one-line version bump. | mechanical: `greps.sh` `HARDCODED_API_PREFIX` |

## What these standards are not

- Not `strict=True` on every pydantic model: upstream and LLM-output schemas stay lax on purpose.
- Not a ban on `Any`: an ORM row inside a schema-level holder may be `Any` so `schemas/` never imports `models/`.
- Not an interface for every class: ports exist at external boundaries only.
- Not a docstring drive: refactors remove paraphrasing docstrings; nothing adds them to meet a count.
- Not a Spring port: the vocabulary stays Python; only the discipline (thin controllers, injected collaborators, one composition root, one transaction owner) carries over.
