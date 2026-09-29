# swarm.json and adapter contracts

The setup directory, `<repo>/docs/swarm/<name>/`, holds everything `swarm.py init` needs. Each
run is created inside it as `<run-id>/`.

```
<setup-dir>/
  README.md         what this swarm is for, how to run and read it
  verdict.md        fitness review; must contain a line `VERDICT: GO`
  swarm.json        the manifest below
  themes.md         the approved theme list with reasoning (copied for the record)
  probe.md          the timing probe and lane-count arithmetic
  adapters/         scripts the manifest's commands call (copied into the run dir and hash-locked)
  data/             optional lane data (gitignored)
  <run-id>/         created by init: locked copy, reports, gitignored runtime state
```

Commands are shell strings run by the orchestrator, never by a lane agent. Placeholders:
`{run_dir}` `{lane}` `{lane_dir}` (worktree root) `{workdir}` (worktree root + `workdir`)
`{commit}` `{seed}`. The same values arrive as env vars `SWARM_RUN_DIR`, `SWARM_LANE`,
`SWARM_LANE_DIR`, `SWARM_WORKDIR`, `SWARM_COMMIT`, `SWARM_SEED`, plus `SWARM_ITERATION` and
`SWARM_LAST_REF` for the result adapter. Adapters live in `{run_dir}/adapters/`.

## Fields

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | short slug; the run id is `<name>-<timestamp>` |
| `mode` | yes | `target_record` or `swarm_eval` (below) |
| `repo` | yes | absolute path of the git checkout |
| `workdir` | yes | subdirectory the target runs from (`"."` for the root); lanes run `claude` here |
| `baseline_commit` | yes | full sha every lane starts from |
| `request` | yes | the human's goal, one paragraph; shown to every lane |
| `iteration_prompt` | yes | target instructions for one iteration: a path relative to `workdir` (must also be in `locked_files`) or `adapters/<file>` written during review |
| `locked_files` | yes | paths relative to `workdir`: objective/scorer, eval harness, holdout access, integrity code, a repo `iteration_prompt`. Hash-checked in every lane after every iteration |
| `result_command` | target_record | prints one JSON line describing the target's latest recorded verdict (contract below) |
| `verify_command` | target_record | prints `{"score": x}` for `{commit}`, run by the swarm in a clean worktree on every ACCEPTED; the ledger keeps this score |
| `verify_tolerance` | no | target_record: how far (fraction) the verified score may fall short of the recorded one before the record is DISPUTED and the swarm halts (default 0.02) |
| `eval_command` | swarm_eval | prints one JSON line scoring the commit checked out in `{workdir}` (contract below) |
| `min_improvement` | no | swarm_eval: margin a candidate must beat the lane's best by (default 0) |
| `maximize` | no | `true` (default) if higher scores are better |
| `lockbox_command` | yes | the one-shot holdout evaluation for a finalist; printed at harvest, never run by the swarm |
| `returns_command` | no | prints `{"returns": [per-period returns...]}` for `{commit}`; enables the deflated Sharpe ratio |
| `reeval_command` | no | prints `{"score": x}` for `{commit}` with `{seed}`; enables the stability re-check |
| `trials.per_iteration_estimate` | yes | trials to charge an iteration whose adapter reports no exact count; overestimate rather than under |
| `trials.note` | yes | where the estimate comes from, e.g. `100 optuna trials x 5 folds x 4 cv = 2000` |
| `lane_setup` | no | commands run once in each new lane's `workdir`; stdout lines `KEY=VALUE` become lane env |
| `env` | no | static env for every lane and adapter |
| `strip_env` | no | env names removed from every lane and adapter (unlock phrases, live credentials) |
| `done_marker` | no | file (relative to `workdir`) whose appearance retires a lane as done |
| `themes` | yes | `[{"id", "title", "scope", "freeze_dirs": [paths relative to workdir]}]` |
| `lanes` | yes | `{"pilot": n, "max": n}` from the probe |
| `models` | yes | `{"pilot_mix": ["sonnet", "opus"], "default": "sonnet"}` |
| `budget` | yes | `{"max_usd", "max_iterations", "max_hours", "stall_k", "iteration_usd_cap"?}` |
| `pilot` | yes | `{"iterations": 50, "thresholds": {"distinct_min": 0.7, "accepted_min": 1, "rejected_max": 0.2}}` |
| `harvest` | no | `{"shortlist": 5, "finalists": 3, "seeds": 3, "min_ratio": 0.5, "dsr_min": 0.95}` |
| `permission_mode` | no | default `auto` |
| `disallowed_tools` | no | extra `--disallowedTools` rules beyond the built-in `git push` / `gh` denials |
| `iteration_timeout_min` / `eval_timeout_min` | no | defaults 120 / 120 |

## Modes

**`target_record`**: the target already runs a loop that decides ACCEPTED / ROLLBACK itself and
commits accepted work (the target's own `loop_cli record`). The lane follows the target's prompt;
afterwards the orchestrator runs `result_command`. A recorded verdict is a claim made while the
lane agent was in control: it could edit the scorer, record, and revert before the lock check,
or write to the target's database directly. So every ACCEPTED commit is re-scored by the swarm
with `verify_command` in a clean worktree. The ledger stores the verified score. A verified
score short of the recorded one (beyond `verify_tolerance`) marks the iteration DISPUTED,
quarantines the lane, and halts the swarm. Verification costs one extra evaluation per ACCEPTED
iteration.

`result_command` prints, as its last stdout line:
```json
{"verdict": "ACCEPTED|ROLLBACK|REJECTED|NONE", "score": 1.23, "ref": "<unique id of the record>",
 "trials": 2000, "hypothesis": "..."}
```
- Read the target's own ledger for the newest record of this lane (lane env, e.g. its loop id),
  newer than `SWARM_LAST_REF`. No new record → `{"verdict": "NONE"}`.
- `score` is required for ACCEPTED. `trials` only when exact; omit it to charge the estimate.

**`swarm_eval`**: the target has an evaluation command but no loop of its own (an agent harness
with an eval suite, a solver with a benchmark). The lane commits one candidate; the
orchestrator tags it `swarm-trials/<run>/<lane>/<n>`, runs `eval_command` on it, keeps it when it
beats the lane's best (or the lane's baseline score) by `min_improvement`, and resets the branch
otherwise. Every trial stays reachable by its tag and its sha in the ledger.

`eval_command` prints, as its last stdout line:
```json
{"score": 0.71, "trials": 1, "detail": {...}}
```
- Exit non-zero only when the evaluation itself could not run; a candidate that breaks the
  target should still score (e.g. 0) when the eval can express that.
- It must evaluate only the checked-out code against the non-holdout set. The holdout is for
  `lockbox_command` alone.

## Example: model research loop (target_record)

```json
{
  "name": "ranker-search", "mode": "target_record",
  "repo": "/Users/you/dev/research-project", "workdir": "engine", "baseline_commit": "<sha>",
  "request": "Improve the ranking model's out-of-sample fitness.",
  "iteration_prompt": "docs/RESEARCH_LOOP.md",
  "locked_files": ["docs/RESEARCH_LOOP.md", "engine/fitness.py",
                   "engine/integrity.py", "engine/lockbox.py"],
  "lane_setup": ["echo LOOP_ID=$(uv run python -m engine.loop_cli start --max 1000 --dataset ...)"],
  "strip_env": ["LOCKBOX_UNLOCK"],
  "result_command": "{run_dir}/adapters/result.sh",
  "verify_command": "{run_dir}/adapters/verify.sh",
  "trials": {"per_iteration_estimate": 2000, "note": "100 trials x 5 wf folds x 4 cv"},
  "lockbox_command": "LOCKBOX_UNLOCK=i-understand-this-is-one-shot uv run python -m ...",
  "done_marker": ".research_loop_done",
  "themes": [{"id": "features", "title": "Feature logic", "scope": "...", "freeze_dirs": ["engine"]}]
}
```

## Example: agent harness (swarm_eval)

```json
{
  "name": "triage-agent", "mode": "swarm_eval",
  "repo": "/Users/you/dev/agent-harness", "workdir": ".", "baseline_commit": "<sha>",
  "request": "Raise the triage agent's pass rate on the dev eval set.",
  "iteration_prompt": "adapters/lane_task.md",
  "locked_files": ["evals/run_eval.py", "evals/dev_set.jsonl", "evals/grader.py"],
  "eval_command": "uv run python evals/run_eval.py --set dev --json | tail -1",
  "trials": {"per_iteration_estimate": 1, "note": "one eval run per candidate"},
  "lockbox_command": "uv run python evals/run_eval.py --set holdout --json",
  "reeval_command": "uv run python evals/run_eval.py --set dev --seed {seed} --json | tail -1",
  "themes": [{"id": "prompts", "title": "System prompt", "scope": "...", "freeze_dirs": ["agent/prompts"]},
             {"id": "tools", "title": "Tool design", "scope": "...", "freeze_dirs": ["agent/tools"]}]
}
```
