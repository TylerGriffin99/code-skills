---
name: python-code-standards
description: Use when a Python repo (especially FastAPI/SQLAlchemy/pydantic) has AI-generated or poorly structured code that must conform to Tyler's structuring ethos — HTTPException in services, fat routers returning dicts, dataclasses outside schemas/, lazy imports, loose client functions, collaborators constructed in place instead of injected, literal status strings instead of enums, repos that commit, long functions with boolean flags, paraphrasing docstrings — or when asked to "apply my python standards", "make this conform", or "clean up this AI code".
---

# Python code standards

Turn a poorly structured Python repo into one that conforms to `references/ethos.md`: **mechanical rules by deterministic check, judgement rules by reviewed refactor**. Nothing is written into the target repo until the plan is approved, and no config or standards file is ever added to it — every check runs from this skill's directory.

`SKILL_DIR` is this file's directory. `REPO` is the target repo root (default: cwd).

## Steps

1. **Detect.** `bash SKILL_DIR/scripts/detect.sh REPO`. Read the `KEY: value` lines: stack, package root, layer dirs, test command, existing guardrails. An existing check is reused, never duplicated; a repo with no lint/CI guardrail is itself a finding. A repo lint config that disagrees with the ethos (e.g. line-length 120) is a Task 1 item.

2. **Mechanical scan.** `bash SKILL_DIR/scripts/check.sh REPO`. Packaged ruff config, an import-linter layering contract generated from the detected layers, and `checks/greps.sh`. Every line is a finding with file:line and rule. No judgement is spent here. Record the count.

3. **Judgement scan.** Dispatch ONE subagent (do it inline when no Agent tool is available) with `references/ethos.md`, the detect output and the mechanical findings. It returns the **candidate inventory**: one row per candidate — file:line, ethos rule, severity, effort, `behaviour-change: yes/no`, `callers:` (grep evidence for every importer of anything renamed, moved or deleted). The inventory MUST contain the rows no linter produces: every `except Exception` classified **fatal vs transient**; every dead module with its importer grep; every policy the code answers implicitly (what happens when all items fail; what an error body says; what an unset credential does).

4. **Baseline.** Run the detected test command once; record pass/fail/skipped and which suites cannot run locally. A **red or unrunnable baseline is the first finding, not something to fix now**: it becomes Task 1. Tooling residue (`.venv`, `uv.lock`, `__pycache__`, `.ruff_cache`) is not a repo edit; list it in the report. For every candidate, name the test that pins its behaviour or mark it `uncovered`; uncovered candidates get a characterisation test in the plan's Characterisation task (the first task that touches application code), never before approval.

5. **Plan and gate.** Write the plan **to a file** (location per `references/plan-template.md`) in template order. **Every step-2 finding and every step-3 candidate maps to a task**; a finding with no task is a plan defect. A task with no candidates is kept as one line `— no candidates, skipped`. Behaviour changes and the policy questions from step 3 are listed as **decisions for the user**; a task that implements one stays in the plan marked `— pending decision D<n>` and runs only if that decision is approved. Present inventory, decisions, and the plan's path. **STOP and wait for approval.** Editing the repo before approval is a violation, whatever the size.

6. **Execute.** `superpowers:subagent-driven-development` (or `superpowers:executing-plans` if asked for inline). Every commit: `bash SKILL_DIR/scripts/check.sh REPO` green, test command green, the user's commit trailer verbatim. Run `silent-failure-hunter` on the diff **right after the error-handling task**, not only at the end.

7. **Finish.** Whole-branch review (`mattpocock-skills:code-review` or `superpowers:requesting-code-review`) plus `silent-failure-hunter` on the full diff. Report: findings fixed, decisions still open, suites that could not run, every ruling made on the user's behalf. Push/PR only when asked.

## Hook mode

`bash SKILL_DIR/scripts/check.sh REPO --hook [files...]` prints findings only and exits non-zero when any exist. Wire as a `PostToolUse` hook on `Edit|Write` for `*.py` or as a `Stop` hook — `hooks/settings-snippet.json`. Nothing is written to the repo in either mode.

## Reference

- `references/ethos.md` — rules, rationale, core vs FastAPI profile, check vs judgement.
- `references/refactor-recipes.md` — before/after and the pinning test for each recurring refactor.
- `references/plan-template.md` — task order, task shape, decision-list shape, gate semantics.
- `checks/` — `ruff.toml`, `importlinter.ini.tmpl`, `greps.sh`.
