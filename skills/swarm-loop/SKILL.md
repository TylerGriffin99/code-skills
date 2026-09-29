---
name: swarm-loop
description: Review whether a goal suits a swarm, then initialise and run parallel research lanes (open-ended search over any target with a locked scorer). Usage /swarm-loop <goal> | status|stop|harvest|scale|cleanup <run-dir>
disable-model-invocation: true
---

# swarm-loop

A **swarm** runs several fresh-context `claude -p` lanes in parallel, each in its own worktree
and theme. Each lane tests one hypothesis per iteration against a locked scorer. A Python
orchestrator (`swarm.py`) owns the ledger, budgets, stall handling, and harvest. It suits
open-ended search where no plan can be written: strategy research, agent-harness tuning, solver
heuristics. If a plan can be written, the right tool is `/ralph-loop`.

`SKILL_DIR` is this file's directory. `SWARM` is `python3 SKILL_DIR/swarm.py`. The manifest schema,
adapter contracts, and two worked examples live in [`MANIFEST.md`](MANIFEST.md). Read it before
step 3.

Argument: `$ARGUMENTS`

## Management commands

If the argument starts with a command word, run it and report. Nothing else in this file applies:

- `status <run-dir>` → `SWARM status`; add a two-line reading of it.
- `stop <run-dir> [--now]` → `SWARM stop`.
- `resume <run-dir>` → `SWARM status`, then `SWARM start` if it is not running. If it is halted,
  show the reason and the evidence. Pass `--clear-halt` only after the human has resolved it
  and said to continue.
- `scale <run-dir>` → show `pilot_report.md`. Ask the human for lanes, model, and any budget
  change, then run `SWARM scale ... && SWARM start <run-dir>`.
- `harvest <run-dir>` → `SWARM harvest`; present `harvest.md`. The lockbox commands in it are for
  the human to run. Offer `SWARM record-lockbox` once they report scores.
- `cleanup <run-dir>` → `SWARM cleanup` (dry run), show it, then `--confirm` on their yes.
- A halt reading "swarm control files changed" after a deliberate skill or adapter update →
  show which files changed; on the human's yes, `SWARM relock <run-dir>`, then resume with
  `--clear-halt`.

Otherwise the argument is a goal. Run the steps below. Everything this skill writes lives in
the target repo, where the human can review and commit it:

- `<repo>/docs/swarm/<name>/` is the **setup dir**: `README.md` (what this swarm is for and how
  to run it), `verdict.md`, `swarm.json`, `themes.md`, `probe.md`, `adapters/`. Data the lanes
  need (e.g. a working-set copy with the holdout removed) goes in `data/`, listed in a
  `.gitignore` there.
- `SWARM init` creates each run inside it: `<setup-dir>/<run-id>/`. Its reports
  (`pilot_report.md`, `harvest.md`) are committed; its locked copy of the setup and its
  runtime state (ledger, logs, pid) are gitignored.

Nothing goes under `~/.claude` except this skill itself.

## 1. Fitness review

Investigate the goal and its target code. Record evidence (file:line, command output), never
impressions. Score every criterion:

| # | Criterion | Kind |
|---|---|---|
| 1 | No plan is possible: the path to a better result is unknown and must be searched | must |
| 2 | A single machine score exists, and lanes cannot edit it (listable as locked files) | must |
| 3 | Results can't be forged: the swarm can score a commit itself with a locked, deterministic-enough eval (as `eval_command` in `swarm_eval`, or `verify_command` to check every ACCEPTED in `target_record`) | must |
| 4 | A holdout exists that lanes cannot reach: sealed in code, gated by an env var the swarm strips, or stored outside the repo | must |
| 5 | Iterations are independent: separate worktrees suffice (check shared state such as databases, caches, fixed ports, and files outside the repo) | must |
| 6 | One iteration's evaluation costs minutes, not hours | should |
| 7 | The search space is wide: at least 3 distinct themes with real extension points | should |
| 8 | The expected value justifies thousands of iterations | should |
| 9 | Trials are countable: exact per iteration, or a declared overestimate from config | must |

Write `verdict.md`: a table of criterion, PASS/FAIL, and evidence. Then add exactly one line:

- `VERDICT: GO`: every must passes. A failed should also passes if the human overrides it; log
  their reason under the table.
- `VERDICT: NOT A SWARM`: criterion 1 fails. Say what to use instead: `/ralph-loop` on a plan,
  or the target's own single loop. Stop.
- `VERDICT: NOT YET`: another must fails. Write a draft prerequisites spec at
  `docs/superpowers/specs/<date>-<name>-swarm-prerequisites-design.md`, one requirement with a
  checkable definition of done per failed criterion. Point the human to grilling → plan →
  `/ralph-loop` on it. Stop. Fixing a scorer, a holdout, or forge-proofing is the human's
  reviewed work, never done inside this skill.

Show the human the verdict with its evidence. Continue only on GO.

## 2. Choose the mode and write adapters

- **`target_record`**: the target already decides ACCEPTED/ROLLBACK from its own runs. Write
  `adapters/result.sh`, which reads the target's ledger for the lane's newest record, and
  `adapters/verify.sh`, which re-scores a commit on the same data the target scored it on. Run
  the verifier twice on the baseline and set `verify_tolerance` from the spread; a
  nondeterministic eval needs a fixed seed.
- **`swarm_eval`**: the target has an eval command but no loop. Write the eval wrapper if one is
  needed, and `adapters/lane_task.md` (the per-iteration instructions: the target's
  conventions, fast test command, and what a good change looks like).

Run each adapter once by hand on the baseline and check its JSON matches the contract in
`MANIFEST.md`. Adapter output decides every verdict, so a wrong adapter is a wrong swarm.

## 3. Themes, probe, manifest

- **Themes.** Read the target's real extension points and propose one theme per distinct
  direction, each with a one-line scope and its `freeze_dirs`. Write `themes.md`.
- **Probe.** Time one real evaluation on the baseline with `/usr/bin/time -l <eval>`. Record
  wall time, CPU time, and peak memory in `probe.md`. Lanes = floor(cores ÷ max(1, CPU time ÷
  wall time)), capped by memory and by budget ÷ (estimated $/iteration × pilot iterations).
  Use `sysctl -n hw.ncpu` for cores. Pilot lanes = min(3, lanes).
- **Manifest.** Write `swarm.json` per `MANIFEST.md`. Put `strip_env` on every unlock or
  credential variable the target knows. Put the scorer, eval harness, holdout access code, and
  any repo iteration prompt in `locked_files`. Set a conservative `trials.per_iteration_estimate`.

**Gate: approval.** Show the human, in one message: the verdict, the mode, the themes, the
lane count with its probe arithmetic, the budget (`max_usd` is mandatory, so ask if it wasn't
given), the pilot size and thresholds, and the locked files. Wait for approval or changes.

## 4. Initialise and launch the pilot

```bash
SWARM init <setup-dir>            # prints the run dir
SWARM start <run-dir>
```

`init` refuses without `VERDICT: GO` or with an invalid manifest; fix the setup and retry.
Brief the human: run dir, lanes and themes, pilot size, and these commands:

- `/swarm-loop status <run-dir>` (or `tail -f <run-dir>/orchestrator.log`)
- `/swarm-loop stop <run-dir>`
- `/swarm-loop scale <run-dir>` after the pilot report lands
- `/swarm-loop harvest <run-dir>` when the run ends

The orchestrator is detached, and `caffeinate` keeps the Mac awake. It survives this session.
It stops by itself at the end of the pilot, writing `pilot_report.md`, and at the end of the
budget.

## Fixed ground

- The swarm never runs a lockbox command and never merges a lane. Both belong to the human.
- The swarm's verdicts come from adapters and the eval, never from a lane agent's own report.
- A halt (locked file changed, broken adapter, repeated lane failure) waits for the human.
  Report its reason; resuming is their call.
