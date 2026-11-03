# Build mode

Start a new project that conforms to `standards.md` from the first commit. There is no plan gate: the gate is `check.sh` and the test command, green after every slice.

## Steps

1. **Pick the profile.** HTTP API → core + FastAPI overlay. CLI, worker, library or data job → core only. If the task does not say, ask once; otherwise default to core + FastAPI when the task mentions endpoints or requests.

2. **Copy the templates** into the empty project directory `DEST` (copy order matters: later overlays replace files):
   ```bash
   cp -R SKILL_DIR/templates/core/. DEST/
   cp -R SKILL_DIR/templates/fastapi/. DEST/          # FastAPI profile only
   cp SKILL_DIR/checks/ruff.toml DEST/ruff.toml        # the project carries its own lint config
   cd DEST && uv add fastapi uvicorn                   # FastAPI profile only
   uv sync && uv run pytest -q && bash SKILL_DIR/scripts/check.sh .
   ```
   Done when the tests pass and `check.sh` prints `CHECK_RESULT: clean`. Rename the package in `pyproject.toml` (`name =`) to the project name; keep the `app/` directory name.

3. **Read the example slice** in `SKILL_DIR/templates/example-slice/` before the first use case. It is one use case end to end (place an order, charge a payment gateway, read it back), and it passes every check on top of core + FastAPI. Copy its shapes, not its domain:
   - port + client + retry classification + idempotency key: `app/clients/payments.py`
   - service with injected repo, port, clock and ID factory; one commit: `app/services/order_service.py`
   - repo that flushes: `app/repos/order_repo.py`
   - `Resources` with a shared HTTP client: `app/bootstrap.py`
   - providers and `Annotated` dependency aliases: `app/dependencies.py`
   - thin route: `app/routes/orders.py`
   - fake, service test on a real DB, client test on `MockTransport`, API test with `dependency_overrides`: `tests/`

4. **Build one use case at a time, outside in by test.** For each use case, in this order:
   1. Schemas (request/response DTOs, `Annotated` scalars) and enums in `constants.py`.
   2. Model + repo, if the use case stores data.
   3. Port + client + fake, if the use case calls an external service. Add the client to `Resources`.
   4. Service: write the unit test first with fakes and the `session` fixture, then the service.
   5. FastAPI profile: provider in `dependencies.py`, route, API test.
   6. Run `uv run pytest -q` and `bash SKILL_DIR/scripts/check.sh .`. Both green before the next use case.

5. **Scripts.** A script in `scripts/` opens resources through the composition root and builds its service the same way a provider does:
   ```python
   async def main() -> None:
       async with open_resources(get_settings()) as resources, resources.session_factory() as session:
           service = OrderService(session=session, orders=OrderRepo(session), payments=resources.payments,
                                  clock=utc_now, new_id=new_id)
           ...

   if __name__ == "__main__":
       asyncio.run(main())
   ```
   Run it as `uv run python -m scripts.<name>`.

6. **README.** Write `README.md` with: how to install, run and test (exact commands); the structure in one paragraph; each design decision with its trade-off (for example "repos are concrete; service tests use a real SQLite DB"); what is left out and what comes next with more time.

7. **Final gate.** `check.sh` clean, tests green, then `silent-failure-hunter` on the whole project. Report the commands run and their results.

## Time-boxed work (interview or spike)

When time is short, keep the order above and cut scope, not structure: fewer use cases, each one complete with its tests. Every port still has a fake; every external call still has its retry classification. Say in the README what you cut.

## Before you hand it over

- [ ] `uv run pytest -q` green; `check.sh` prints `CHECK_RESULT: clean` (ruff, format, mypy --strict, import-linter, greps).
- [ ] Every collaborator is a constructor parameter; constructors run only in `bootstrap.py`, `dependencies.py`, `main.py`, `scripts/`.
- [ ] One pooled HTTP client per external service, closed at shutdown.
- [ ] Every external boundary has a Protocol port and a fake in `tests/fakes/`; nothing else has an interface.
- [ ] Every retry has a test for retry count and for fatal = one call; every retried POST has an idempotency key.
- [ ] Domain errors carry no HTTP status; every error body is `{"detail": ...}` with no SQL.
- [ ] Each service method commits once; repos only flush.
- [ ] No string literal for a closed set; no naive datetime.
- [ ] README states run commands, decisions with trade-offs, and what is left out.
- [ ] No leftover example-slice code that the task does not need.
