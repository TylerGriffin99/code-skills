# Plan template

Write the plan to a FILE: the repo's plan location if one exists (e.g. `docs/superpowers/plans/<date>-<topic>.md`), otherwise the session scratchpad. An inline plan cannot be executed by `superpowers:subagent-driven-development`, which reads the file. Its header line, verbatim: `> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.` Present the path, not the body.

**Red baseline:** when step 4 found the test command failing or unrunnable, Task 1 is "make the repo's own tests run" (pythonpath, missing dev deps, env file) — the exact failure text goes in the task. Editing the repo's own `pyproject.toml` test section for this is in scope; adding lint/standards config that duplicates `checks/` is not. Nothing else may start until it is green.

**Missing layers:** when `detect.sh` reports a layer as `none`, the plan creates it at its path in the structure in `standards.md` (`routes/`, `services/`, `repos/`, `schemas/`, `models/`, `database/`, `clients/`, `utils/`, `constants.py`, `exceptions.py`, `bootstrap.py`, and for FastAPI `dependencies.py`, `error_handlers.py`). Copy the file from `templates/` when one exists. Name the path in the task that creates it.

## Order of tasks

Dependency order that keeps every commit green and bisectable. A task with no candidate in the inventory stays in the plan as a single line `### Task N: <name> — no candidates, skipped` so the numbering and the reader's expectations hold; never reorder.

1. **Tooling baseline** — the repo's own lint/type/test commands (`just check`, `make check`, or document the exact commands), plus the red-baseline fix when there is one. Do NOT add lint/standards config files; the packaged `check.sh` is the gate.
2. **Format** — if the repo is not ruff-formatted, run the formatter as its **own commit** (large, mechanical, must stay bisectable from real changes).
3. **Characterisation tests** — pin current behaviour of every candidate that lacks a covering test (recipe pins in `refactor-recipes.md`). Own commit.
4. **Dead code sweep** — delete the certain set with importer evidence; list the uncertain set for the user. Early, so later tasks never work around dangling imports.
5. **Constants and enums** — `constants.py`: one `StrEnum` per closed string set found by `PLR2004`/`LITERAL_SET_COMPARE`, `Final` for magic numbers, `Annotated` scalar aliases in `schemas/common.py`. Replace literals at every site (recipe 12). Own commit; wire format unchanged.
6. **Shared utilities** — copy from `templates/core`: `StrictModel`, `RetryPolicy` + `retry_async`, `bounded_gather`, `Clock`/`IdFactory`, `UtcDateTime`, `require`, with their tests; `load_prompt` if the repo has prompts. Own commit.
7. **Composition root + clients** — `bootstrap.py` with `Resources` and `open_resources`; one port + client class per upstream with a pooled HTTP client built in `open_resources` (recipe 4); one fake per port in `tests/fakes/`. Delete the old module only after grepping tests for its seam names.
8. **Orchestration rewrite** — the module that used closures/private helpers, on top of 6 and 7 (recipes 5, 7). Preserve every log substring tests assert. **Run `silent-failure-hunter` on the diff after this task.**
9. **Errors** — `exceptions.py` (+ `require`) + `error_handlers.py` + `session_scope` rollback (table test from `templates/fastapi`), then services → domain errors (message text byte-identical; `require()` at every `if not x: raise` site — recipes 1, 10).
10. **Injection + routes + DTOs** — constructor injection and providers for every `CONSTRUCTED_IN_PLACE`, `HTTP_CLIENT_PER_CALL` and `CLOCK_NOT_INJECTED` finding (recipe 11); thin routes, new service methods for logic that lived in routes, `response_model` everywhere (recipe 2); one commit per use case (`COMMIT_OUTSIDE_SERVICE`). JSON shape compared old vs new in the task report.
11. **Schemas** — remaining dataclasses → `StrictModel` in `schemas/` or plain handle-holder classes (recipe 3); DB model audit (report only unless something is off).
12. **Versioning** — config-driven prefix; tests, scripts, seeds, frontend.
13. **Types and conciseness** — every `MYPY` and `ANN` finding; every `C901`/`PLR09xx`/`FBT`/`RET`/`PERF` finding: split, bundle args into a `StrictModel`, make booleans keyword-only, early-return; delete paraphrasing docstrings and `what` comments (`ERA001`, `T20`). No behaviour change; pinned by the characterisation tests from Task 3.
14. **Verification** — full gate, whole-branch review, hunter on the full diff, hand-off summary with every ruling.

## Task shape

```
### Task N: <name>
**Files:** Create / Modify (by text anchor, not line number — the formatter moves lines) / Test
**Interfaces:** Consumes (exact signatures from earlier tasks) / Produces (exact names + types later tasks use)
**Steps:** failing test → run (RED) → implement → run (GREEN) → check.sh + repo test command → commit `<type>(<scope>): <subject>` + the user's trailer
```
Every step carries real code or exact commands. "Similar to Task 3" and "add appropriate error handling" are plan failures.

## Decisions for the user (not tasks)

List before the plan, one line each, with the default you'd pick and what it costs if wrong:
- Behaviour changes the refactor would introduce (URL moves, status code changes, body shape changes).
- Policy the code currently answers implicitly: what happens when every item fails; what an error body may say (driver message vs fixed string); what an unset credential does (silent fallback vs fail fast).
- The uncertain dead-code set.
- Any scoping/authorisation gap noticed in passing (path params not used to scope a query) — flag, do not fix silently.

## Decision → task mapping

Every decision's **default** names the task step that implements it (`D3 → Task 9 Step 2`). A decision whose default has no step is a plan defect: in the first run, D3's "validate the provider key at boot" was approved and never scheduled, and only the whole-branch review caught it. Check this mapping in the plan's self-review before presenting the gate.

## Gate

Present inventory + decisions + plan. Stop. The user's reply approves the plan as written; a reply that answers decisions but says nothing about the plan approves the decisions only.
