---
name: python-code-standards
description: Use when writing or reviewing Python in Tyler's style — starting a new project (FastAPI API, CLI, worker, interview or take-home test), adding a feature to one, or bringing an existing repo into line ("apply my python standards", "make this conform", "clean up this AI code"). Covers project structure, dependency injection and a composition root, Protocol ports with fakes, shared API clients, strict types, domain errors, transaction ownership and the packaged ruff/mypy/import-linter checks.
---

# Python code standards

`references/standards.md` is the single source of truth: project structure, every rule, its reason, and whether a check or a reviewer enforces it. Read it before you write or judge any code. Both modes below apply it; neither restates it.

`SKILL_DIR` is this file's directory. `REPO` is the target repo root (default: cwd).

## Pick the mode

- **Build** — the project is new or nearly empty, or you add a use case to a project that already conforms. Follow `references/build.md`: copy `templates/core` (+ `templates/fastapi` for an HTTP API), build one use case at a time from the patterns in `templates/example-slice`, gate every use case on `check.sh` and tests.
- **Refactor** — the repo exists and does not conform. Follow `references/refactor.md`: detect, scan, inventory, approval-gated plan, execute.

When unsure, run `bash SKILL_DIR/scripts/check.sh REPO`: a clean or near-clean result means build mode; many findings mean refactor mode.

## The gate (both modes)

`bash SKILL_DIR/scripts/check.sh REPO` runs ruff (packaged `checks/ruff.toml`), `ruff format --check`, `mypy --strict` (packaged `checks/mypy.ini`, against the repo's `.venv`), import-linter contracts generated from the detected layers, and `checks/greps.sh`. It prints `CHECK_RESULT: clean` or the findings with file:line and rule, and it writes nothing into the repo. Green means `CHECK_RESULT: clean` **and** the repo's test command passing.

**Hook mode:** `check.sh REPO --hook [files...]` prints findings only (ruff, import-linter, greps; no format or mypy) and exits non-zero when any exist. Wire it as a `PostToolUse` hook on `Edit|Write` or as a `Stop` hook — `hooks/settings-snippet.json`.

## Files

- `references/standards.md` — the rules.
- `references/build.md` — build steps, time-boxed variant, hand-over checklist.
- `references/refactor.md`, `references/refactor-recipes.md`, `references/plan-template.md` — refactor mode.
- `templates/core`, `templates/fastapi`, `templates/example-slice` — tested starting points; `scripts/verify-templates.sh` proves every combination passes the gate (run it after you change a template or a check).
- `checks/` — `ruff.toml`, `mypy.ini`, `greps.sh`.
