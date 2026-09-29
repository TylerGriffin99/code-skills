# swarm.json and contracts

The setup directory, `<repo>/docs/swarm-collab/<name>/`, holds everything `collab.py init` needs.
Each run is created inside it as `<run-id>/`.

```
<setup-dir>/
  verdict.md        fitness review; must contain a line `VERDICT: GO`
  swarm.json        the manifest below
  adapters/         optional scripts your eval commands call (copied into the run dir, chmod +x,
                     and hash-locked as control files)
  <run-id>/         created by init: locked copy of verdict.md/swarm.json/adapters/, plus
                     board.jsonl, ledger.sqlite, locks.json, logs/, drops/, prompts/, and the two
                     reports meant to be kept: pilot_report.md, harvest.md
```

`collab.py init` copies `verdict.md` and `adapters/` into the run dir, writes `run_id` into its own
copy of `swarm.json`, and hash-locks the whole set (see "Locking" below). Only `pilot_report.md` and
`harvest.md` are meant to be committed; everything else inside `<run-id>/` is local runtime state
(the run dir's own `.gitignore`, written by `init`, keeps it that way).

## Fields

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | short slug; the run id is `<name>-<timestamp>` |
| `repo` | yes | absolute path of the git checkout; must exist as a directory |
| `workdir` | yes | subdirectory `locked_files` paths are relative to (`"."` for the repo root). **Not** joined onto the worktree path for eval commands — see "The eval contract" below |
| `baseline_commit` | yes | full sha every implementor's worktree resets to at the start of every round (implementors do not carry a winning commit forward between rounds in this version — see "Rounds" below) |
| `goal` | yes | the human's goal, one paragraph; shown to every role via the pinned facts at the top of its view |
| `maximize` | no | `true` (default) if a higher score is better; flips every best/target/promote comparison |
| `target_score` | no | once the best recorded `full_score` clears it, the run stops (`termination_reason` returns `"target score reached"`) |
| `roles` | yes | `{"strategist": n, "implementor": n, "critic": n}`; each `n` is an integer `>= 0` except `implementor`, which must be `>= 1`. Agents are named `<role>-1..<role>-n` |
| `budget` | yes | `{"max_usd", "max_rounds", "max_hours", "round_token_cap", "agent_token_cap"}` — see "Budget: what's enforced today" below |
| `view` | yes | `{"token_ceiling": n, "compact_threshold_entries": n}` — `token_ceiling >= 500`; the per-agent view renderer trims lowest-priority entries until its estimate fits under this ceiling. `compact_threshold_entries` is how many live (non-digest) entries accumulate before the board is compacted into a digest |
| `messaging` | yes | `{"per_turn_quota": n, "max_tokens": n}` — caps on `message`-type drop entries per agent turn; see "The agent drop-file contract" |
| `eval` | yes | `{"proxy_command", "full_command", "verify_tolerance", "promote_threshold"?}` — see "The eval contract" |
| `rounds` | yes | `{"strategist_every": n, "critic_every": n, "council_every": n}`, each `>= 1`. A role's turns run on rounds where `round_no % n == 0`. Optional `stall_rounds` (default `3`, not schema-checked) controls the no-improvement stall window |
| `pilot` | yes | `{"rounds": n}`, `n >= 1` and `<= budget.max_rounds`. `pilot_report.md` is written once, the first time `round_no >= pilot.rounds` |
| `locked_files` | yes | paths relative to `workdir`: the scorer, eval/proxy harness, holdout access code, and anything else agents must not edit. Hash-checked against `baseline_commit` at `init`/`write_locks` time, and against every implementor's worktree after every round |
| `strip_env` | yes | env var names removed from every role agent's process (unlock phrases, live credentials); combined with an unconditional strip of every `COLLAB_*` var |
| `models` | no | per-role model override, e.g. `{"strategist": "opus", "implementor": "sonnet", "critic": "sonnet"}`. A role with no entry falls back to top-level `model`, then to the literal string `"default"` |
| `model` | no | single fallback model when `models` doesn't name a role |
| `permission_mode` | no | default `"auto"`; passed to `claude -p --permission-mode` |
| `eval_timeout_min` | no | default `120`; timeout for `eval.proxy_command`/`eval.full_command` |
| `iteration_timeout_min` | no | default `120`; timeout the orchestrator waits for a role's turn before killing it |
| `run_id` | auto | written by `collab.py init` into the run dir's copy of `swarm.json`; do not set it in your draft |

## Budget: what's enforced today

All five caps now bind, because each turn is reaped when its process finishes: its
`total_cost_usd` and `usage.output_tokens` (from the `claude -p --output-format json` summary log)
are written back onto its `turns` row, so the accounting the caps depend on (`budget.totals`,
`budget.round_tokens`) is populated for every completed turn.

- **`max_rounds` and `max_hours`** — checked every round by `budget.budget_reason`; the run halts
  when either is reached.
- **`max_usd`** — also checked by `budget.budget_reason` against the summed per-turn `cost_usd`; the
  run halts once total spend reaches it.
- **`round_token_cap`** — checked before each turn is launched (`budget.round_cap_reason`): once a
  round's summed tokens reach it, no further turns launch that round.
- **`agent_token_cap`** — checked before launching a given agent (`budget.agent_cap_exceeded`): an
  agent already at/over its per-round token cap is skipped for the rest of that round.

A turn whose agent process never reports usage (no parseable summary log) records what is present
and logs a `turn_usage_missing`/`turn_usage_partial` event; its untracked spend simply does not
count toward the token/cost caps, so treat a run full of those events as unmetered rather than free.

## The blackboard

`<run-dir>/board.jsonl` is an append-only log; each line is one entry:

```
{"id": 7, "round": 2, "role": "strategist", "author_agent": "strategist-1",
 "ts": "2026-09-28T10:15:03", "type": "proposal", "refs": [], "to": [],
 "body": {"text": "Try a residual connection after block 2; distinct from #4's dropout change."}}
```

- `id`: assigned by the orchestrator (`max_id() + 1`); never set this yourself.
- `round`, `role`, `author_agent`, `ts`: filled in by the orchestrator from the turn that produced
  the entry.
- `type`: one of `hypothesis`, `proposal`, `result`, `critique`, `deadend`, `message`, `council`,
  `digest`.
- `refs`: ids of other entries this one is about (a `critique`'s target proposal/result, a
  `digest`'s archived entries).
- `to`: agent names this entry is addressed to (only meaningful for `message`; used to route it
  into the addressee's view).
- `body`: shape depends on `type` (below). The renderer treats `body.text` as the one-line summary
  when present, and `body.score` (only meaningful on `result`) as informational — it is never the
  score the swarm acts on; see "The eval contract".

### Body shapes by type

| Type | Typical body | Notes |
|---|---|---|
| `hypothesis` | `{"text": "..."}` | An early idea note, posted by any role |
| `proposal` | `{"text": "...", "distinct_from": "..."}` | A strategist's proposed direction. `distinct_from` is a convention, not a schema field — state how it differs from open proposals so implementors and critics can tell your proposals apart |
| `result` | `{"text": "...", "score": 0.71}` | An implementor's report on its turn's commit. `score` here is a claim; the swarm never reads it — see "The eval contract" |
| `critique` | `{"text": "..."}`, `refs: [proposal_or_result_id]` | A critic's challenge to a specific proposal or result |
| `deadend` | `{"text": "..."}` | A critic's prediction that a direction won't pay off, with its reasoning |
| `message` | `{"text": "..."}`, `to: ["implementor-2"]` | Quota-limited — see "The agent drop-file contract" |
| `council` | `{"text": "...", "terminate": true}` | Posted only during a council round (`rounds.council_every`); `terminate: true` from either the council strategist or council critic ends the run early (never a success verdict on its own — see below) |
| `digest` | `{"text": "..."}`, `refs: [archived ids]` | Written by the orchestrator's compactor (`role: "system"`, `author_agent: "compactor"`), never by an agent |

## The agent drop-file contract

Each role's turn is launched with an environment variable `COLLAB_DROP` pointing at a per-turn file
(`<run-dir>/drops/<role>-<agent>-r<round>.jsonl`) that does not exist yet. To post to the blackboard,
write one JSON object per line to that file — never to `board.jsonl` directly:

```
{"type": "proposal", "body": {"text": "Try widening the first conv layer before adding depth."}}
{"type": "message", "body": {"text": "Heads up: your last commit reverted the batchnorm fix."}, "to": ["implementor-2"]}
```

- Fields: `type` (required, one of the board types above, though posting a `digest` yourself has no
  effect — only the compactor's own call creates one), `body` (required, a dict), `refs` (optional
  list of entry ids), `to` (optional list of agent names).
- Do not include `id`, `round`, `role`, `author_agent`, or `ts` — the orchestrator fills those in
  when it ingests the file.
- **Message quota.** Two independent rules apply to `message` entries only. First, a count quota:
  each `message` counts against `messaging.per_turn_quota` — only the first N accepted per turn
  survive; extras are dropped. Second, a per-message size cap: any individual `message` whose
  estimated size (roughly `len(text) / 4` tokens) exceeds `messaging.max_tokens` is dropped as
  oversized, regardless of remaining quota. Both kinds of drop are logged as a `message_dropped`
  event, visible in `status`. Non-`message` entries are never quota-limited.
  Order matters: the count quota is consumed in file order, so put your most important message first.
- If the drop file is missing when the orchestrator goes to ingest it (the agent crashed or wrote
  nothing), that turn simply contributes zero entries (`event: drop_missing`) — it is not treated as
  a failure by itself. A malformed line (invalid JSON, or missing `type`) raises loudly and is not
  swallowed.
- **Nothing you write to `COLLAB_DROP` is ever trusted as a score.** A `result` entry's `body.score`
  is a claim for other agents to read; the swarm only acts on the score its own locked eval command
  computes.

## The eval contract

`eval.proxy_command` and `eval.full_command` are shell strings the **orchestrator** runs — never an
agent — against a specific commit in a specific worktree:

- `cwd` is the worktree root (an implementor's persistent worktree during a normal turn, or a fresh
  worktree during `verify`). **The manifest's `workdir` is not joined onto this path** — if your
  target lives in a subdirectory, `cd` into it yourself inside the command (e.g. via
  `"cd \"$COLLAB_WORKDIR/$SUBDIR\" && ..."`, or make your adapter script do it).
- Env vars: `COLLAB_COMMIT` (the commit sha being scored), `COLLAB_WORKDIR` (the worktree root, same
  as `cwd`), `COLLAB_RUN_DIR` (the run dir — use this to reference `adapters/` scripts, e.g.
  `"$COLLAB_RUN_DIR/adapters/proxy_eval.sh"`), `COLLAB_AGENT` (the agent being scored, or `"verify"`
  during a `verify` call), and `COLLAB_ROUND` (the round number, only set when scoring a turn, not
  during `verify`).
- **Contract:** print exactly one JSON object as the *last line* of stdout, with a numeric `"score"`
  key. Other keys (e.g. `"detail"`) are yours to add and are not read by the swarm. Exit non-zero,
  or a last line that isn't JSON or has no numeric `score`, raises and halts the run — a candidate
  that legitimately can't be evaluated should still print a score your eval can express (e.g. `0`),
  not fail the process.
- `eval_timeout_min` (default 120) bounds how long the orchestrator waits for either command.

Two-tier scoring, run by `evaluate.score_result` after every implementor turn:

1. Run `proxy_command`; record its score as `proxy_score`.
2. Promote (also run `full_command` and record `full_score`, `promoted=1`) when the proxy score
   clears `eval.promote_threshold` — or, if you didn't set one, when it beats the best `proxy_score`
   recorded so far across the whole run (respecting `maximize`).

`evaluate.verify` separately re-runs `full_command` on a commit and compares the new score against a
previously recorded one; `ok` is `abs(new_score - recorded) <= eval.verify_tolerance`.

## Rounds

Each round runs, in order: strategist turns (if `round_no % rounds.strategist_every == 0`), critic
turns (if `round_no % rounds.critic_every == 0`), every implementor's turn in parallel (each scored
per "The eval contract"), then a council turn (if `round_no % rounds.council_every == 0`). Every
implementor's worktree resets to `baseline_commit` at the start of its turn each round in this
version — implementors do not build on a previous round's winning commit; the blackboard, not a
carried-forward commit, is how one round's findings reach the next.

## Locking

`locked_files`, the manifest/verdict/adapters/`roles/*.md`/the `swarmcollab` package itself
("control files"), and the blackboard's already-written prefix are all hash-checked every round. A
change to any of them halts the run for a human (`collab.py relock` to accept a deliberate change,
then resume with `--clear-halt`). See `SKILL.md`'s "Fixed ground" for what this guarantees.

## Worked example (a): CNN vs. a golden dataset, small-subset proxy

A classifier where a full validation pass takes minutes, but a small labeled subset gives a fast,
representative proxy signal, and a slower full-dataset pass promotes and confirms.

```json
{
  "name": "cnn-golden",
  "repo": "/Users/you/dev/cnn-classifier",
  "workdir": ".",
  "baseline_commit": "4f2c9d1a8b3e5f70c61d2a9b5e8f0123abc45671",
  "goal": "Raise the CNN classifier's accuracy on the golden validation set without touching the golden dataset, the scorer, or the holdout split.",
  "maximize": true,
  "target_score": 0.94,
  "roles": {"strategist": 1, "implementor": 3, "critic": 1},
  "budget": {"max_usd": 40.0, "max_rounds": 30, "max_hours": 6, "round_token_cap": 400000, "agent_token_cap": 60000},
  "view": {"token_ceiling": 6000, "compact_threshold_entries": 60},
  "messaging": {"per_turn_quota": 2, "max_tokens": 300},
  "eval": {
    "proxy_command": "$COLLAB_RUN_DIR/adapters/proxy_eval.sh",
    "full_command": "$COLLAB_RUN_DIR/adapters/full_eval.sh",
    "verify_tolerance": 0.01,
    "promote_threshold": 0.85
  },
  "rounds": {"strategist_every": 1, "critic_every": 1, "council_every": 5, "stall_rounds": 6},
  "pilot": {"rounds": 5},
  "locked_files": ["eval/golden_val.py", "eval/golden_val_data.npz", "eval/full_eval.py", "eval/holdout_guard.py"],
  "strip_env": ["CNN_HOLDOUT_KEY"],
  "models": {"strategist": "opus", "implementor": "sonnet", "critic": "sonnet"},
  "permission_mode": "auto"
}
```

`adapters/proxy_eval.sh` runs the model checked out at `$COLLAB_COMMIT` against a fixed 200-image
subset of the golden set and prints `{"score": <accuracy>}`. `adapters/full_eval.sh` runs the same
model against the full golden set. Because `promote_threshold` (0.85) is set, only candidates that
clear it on the proxy pay for a full-dataset pass; `target_score` (0.94) is checked against
`full_score`, so the run only ends "on target" once a promoted candidate's full-dataset score clears
it.

## Worked example (b): agent-harness accuracy search, fast few-task proxy

An agent harness with a dev eval set large enough that scoring all of it every turn is slow; a small
random sample gives a cheap proxy, and the full dev set is the promotion/full score (no separate
`promote_threshold`, so promotion is "beats the best proxy score seen so far this run").

```json
{
  "name": "triage-agent-collab",
  "repo": "/Users/you/dev/support-triage-agent",
  "workdir": ".",
  "baseline_commit": "9a1b2c3d4e5f60718293a4b5c6d7e8f901234568",
  "goal": "Raise the support triage agent's pass rate on the dev eval set by improving its system prompt and tool routing.",
  "maximize": true,
  "roles": {"strategist": 1, "implementor": 2, "critic": 1},
  "budget": {"max_usd": 15.0, "max_rounds": 20, "max_hours": 3, "round_token_cap": 250000, "agent_token_cap": 40000},
  "view": {"token_ceiling": 4000, "compact_threshold_entries": 40},
  "messaging": {"per_turn_quota": 1, "max_tokens": 200},
  "eval": {
    "proxy_command": "uv run python evals/run_eval.py --set dev --sample 8 --json | tail -1",
    "full_command": "uv run python evals/run_eval.py --set dev --json | tail -1",
    "verify_tolerance": 0.02
  },
  "rounds": {"strategist_every": 1, "critic_every": 2, "council_every": 4},
  "pilot": {"rounds": 4},
  "locked_files": ["evals/run_eval.py", "evals/dev_set.jsonl", "evals/grader.py"],
  "strip_env": []
}
```

Here the eval commands are plain shell (no `adapters/` wrapper needed) and run directly in the
worktree root, since `workdir` is `"."`. Without `promote_threshold`, the first implementor result
every run is promoted automatically (there is no prior best to beat), and afterwards only strictly
better proxy scores trigger a full-dataset run — keep this in mind if your proxy is noisy: a lucky
proxy score promotes and burns a full-eval run even if the full score doesn't confirm it.
