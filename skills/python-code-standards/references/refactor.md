# Refactor mode

Bring an existing repo into line with `standards.md`: **mechanical rules by deterministic check, judgement rules by reviewed refactor**. Nothing is written into the target repo until the plan is approved, and no config or standards file is added to it — every check runs from the skill directory.

## Steps

1. **Detect.** `bash SKILL_DIR/scripts/detect.sh REPO`. Read the `KEY: value` lines: stack, package root, layer dirs, test command, `.venv`, existing guardrails. An existing check is reused, never duplicated; a repo with no lint/CI guardrail is itself a finding. A repo lint config that disagrees with the standards (e.g. line-length 88) is a Task 1 item. A layer reported `none` is created at its path from the structure in `standards.md` by the task that first needs it.

2. **Mechanical scan.** `bash SKILL_DIR/scripts/check.sh REPO`: packaged ruff config, `mypy --strict` against the repo's `.venv`, import-linter contracts generated from the detected layers, and `checks/greps.sh`. Every line is a finding with file:line and rule. Record the `CHECK_FINDINGS` count. `TYPECHECK_SKIPPED` means no interpreter was found: run the repo's own install command, or set `PCS_PYTHON`, and rerun before you continue. `TOOL_ERROR` means a check did not run: fix that before you trust any count.

3. **Judgement scan.** Dispatch ONE subagent (do it inline when no Agent tool is available) with `standards.md`, the detect output and the mechanical findings. It returns the **candidate inventory**: one row per candidate — file:line, standards rule, severity, effort, `behaviour-change: yes/no`, `callers:` (grep evidence for every importer of anything renamed, moved or deleted). The inventory MUST contain the rows no linter produces: every `except Exception` classified **fatal vs transient**; every collaborator built in place with the provider it moves to; every external call with its retry and idempotency status; every dead module with its importer grep; every policy the code answers implicitly (what happens when all items fail; what an error body says; what an unset credential does).

4. **Baseline.** Run the detected test command once; record pass/fail/skipped and which suites cannot run locally. A **red or unrunnable baseline is the first finding, not something to fix now**: it becomes Task 1. Tooling residue (`.venv`, `uv.lock`, `__pycache__`, `.ruff_cache`) is not a repo edit; list it in the report. For every candidate, name the test that pins its behaviour or mark it `uncovered`; uncovered candidates get a characterisation test in the plan's Characterisation task, never before approval.

5. **Plan and gate.** Write the plan **to a file** (location and order per `plan-template.md`). **Every step-2 finding and every step-3 candidate maps to a task**; a finding with no task is a plan defect. Behaviour changes and the policy questions from step 3 are **decisions for the user**; a task that implements one is marked `— pending decision D<n>`. Present inventory, decisions, and the plan's path. **STOP and wait for approval.** Editing the repo before approval is a violation, whatever the size.

6. **Execute.** `superpowers:subagent-driven-development` (or `superpowers:executing-plans` if asked for inline). Every commit: `check.sh` green for the files it touches, test command green, the user's commit trailer verbatim. Run `silent-failure-hunter` on the diff **right after the error-handling task**, not only at the end.

7. **Finish.** Whole-branch review (`mattpocock-skills:code-review` or `superpowers:requesting-code-review`) plus `silent-failure-hunter` on the full diff. Report: findings fixed, decisions still open, suites that could not run, every ruling made on the user's behalf. Push/PR only when asked.

## Reference

- `refactor-recipes.md` — before/after and the pinning test for each recurring refactor.
- `plan-template.md` — task order, task shape, decision list, gate semantics.
