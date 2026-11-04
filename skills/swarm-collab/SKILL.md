---
name: swarm-collab
description: Review whether a goal suits a collaborative swarm, then run many communicating claude -p agents (strategist/implementor/critic) that share a bounded blackboard to search an unplannable build-and-measure problem against a locked scorer. Usage /swarm-collab <goal> | status|stop|resume|scale|harvest|cleanup|board|relock <run-dir>
disable-model-invocation: true
---

# swarm-collab

A **swarm-collab run** launches several fresh-context `claude -p` agents in three roles —
strategist, implementor, critic — that read and write a shared, bounded blackboard instead of
working in isolation. Implementors each test one change per round in their own worktree against a
locked, two-tier eval (a cheap proxy, plus a full score for promoted candidates); strategists
propose directions from what the board shows; critics challenge proposals and flag dead ends before
compute is spent on them, and write a retro after each round's commits are scored. A Python orchestrator (`collab.py`) owns the board, the budget, the
locking, and the harvest. It suits open-ended search where no plan can be written *and* where
agents reacting to each other's proposals and critiques plausibly beats blind parallel search: CNN
architecture search against a golden dataset, agent-harness prompt/tool tuning against an eval set.
If collaboration wouldn't add anything over independent lanes, `/swarm-loop` is the right tool; if a
plan can be written at all, it's `/ralph-loop`.

`SKILL_DIR` is this file's directory. `COLLAB` is `python3 SKILL_DIR/collab.py`. The manifest
schema, the blackboard entry envelope, the agent drop-file contract, and the eval contract — with
two worked examples — live in [`MANIFEST.md`](MANIFEST.md). Read it before step 3. The per-role
operating contracts live in `roles/strategist.md`, `roles/implementor.md`, `roles/critic.md`, and
`roles/retro.md`.

Argument: `$ARGUMENTS`

## Management commands

If the argument starts with a command word, run it and report. Nothing else in this file applies:

- `status <run-dir>` → `COLLAB status`; add a two-line reading of it (round/budget position, best
  score so far).
- `stop <run-dir> [--now]` → `COLLAB stop`.
- `resume <run-dir>` → `COLLAB status` first. If it's halted, show the reason and the evidence
  behind it (recent events, which lock tripped); pass `--clear-halt` only after the human has
  resolved the cause and explicitly said to continue. If it's simply not running, `COLLAB start
  <run-dir>` relaunches it.
- `scale <run-dir>` → show `pilot_report.md`. Ask the human for any budget change (note in
  `MANIFEST.md`: `max_rounds`/`max_hours` are the caps that reliably stop a run today; `max_usd` and
  the token caps are configured but not yet enforced), then `COLLAB scale [--max-usd X]
  [--max-rounds N] [--max-hours H] <run-dir> && COLLAB start <run-dir>` (add `--clear-halt` if
  halted).
- `harvest <run-dir>` → `COLLAB harvest`; present `harvest.md` — best scores, per-role spend, and
  the collaboration-ROI note (whether critiqued proposals actually outscored un-critiqued ones).
  This skill defines no holdout/lockbox command; any further holdout confirmation of the winning
  commit is the human's own follow-up, outside `collab.py`.
- `cleanup <run-dir> [--confirm]` → `COLLAB cleanup` (dry run by default: lists implementor
  worktrees/branches it would remove), show it, then `--confirm` on the human's yes.
- `board <run-dir> [--limit N]` → `COLLAB board`; the most recent blackboard entries.
- A halt reading "control files changed" after a deliberate skill/manifest/adapter update → show
  which files changed (`COLLAB status` lists the halt reason); on the human's yes, `COLLAB relock
  <run-dir>`, then resume with `--clear-halt`.

Otherwise the argument is a goal. Run the steps below. **All swarm work for a project lives in one
results folder beside it**, `<project-parent>/<project>-swarm-results/` (for a project at
`~/dev/acme`, that is `~/dev/acme-swarm-results/`). Nothing is written next to the project or
anywhere else:

```
<project>-swarm-results/
  <name>/                 the target repo for one search (a git repo; see below)
    swarm/                the setup dir: verdict.md, swarm.json, optional adapters/
      <run-id>/           created by COLLAB init: the run dir
        board.jsonl       THE record of the run (see "The board" below)
        worktrees/        one git worktree per implementor (and the baseline), removed after use
        ledger.sqlite, locks.json, logs/, drops/, prompts/
        pilot_report.md, harvest.md
```

- **The target repo `<name>/`.** If the search edits an existing project repo, make it a git
  worktree of that repo on a new branch: `git -C <project> worktree add
  <results>/<name> -b swarm/<name> <base>`. The project checkout itself is never touched. If the
  search needs a purpose-built repo (a locked harness, a seed file, train data), `git init` it at
  `<results>/<name>/`.
- **The setup dir** `<name>/swarm/` holds `verdict.md`, `swarm.json`, and an optional `adapters/`
  for eval helper scripts. Full field reference in `MANIFEST.md`. Commit it before `init`.
- **The run dir** `<setup-dir>/<run-id>/`: its reports (`pilot_report.md`, `harvest.md`) are meant
  to be committed; everything else, the implementor worktrees included, is gitignored by a
  `.gitignore` `init` writes into the run dir.
- Data the agents must not reach (a holdout) stays outside the results folder.

Nothing goes under `~/.claude` except this skill itself.

### The board

`<run-dir>/board.jsonl` is the single record an agent (or the human) needs to make a call. In round
order it holds:

1. The **baseline** `scored` entry (round 0): the orchestrator scores `baseline_commit` with the
   locked evals before the first turn.
2. Each round's **proposals**, **critiques** and **dead ends** (strategist, critic).
3. Each implementor's **result** (its own account, with refs to the proposal it tested).
4. One orchestrator **`scored`** entry per implementor commit: the commit sha, the locked eval's
   score (proxy, and full if promoted), the eval's own detail (its JSON keys, or `eval.board_keys`),
   and the change against the baseline and the best score before it. Agents cannot post this type,
   and a `scored` line not recorded in the ledger is never shown.
5. One **`retro`** entry (the critic's model, `roles/retro.md`): what each scored change taught,
   with shas, and what to build on or stop next.

Every role's view leads with the latest retros and the baseline, best and latest `scored` entries;
compaction never archives `scored` or `retro` entries.

## 1. Fitness review

Investigate the goal and its target code. Record evidence (file:line, command output), never
impressions. Score every criterion:

| # | Criterion | Kind |
|---|---|---|
| 1 | No plan is possible: the path to a better result is unknown and must be searched | must |
| 2 | A single machine score exists, and implementors cannot edit it (listable as `locked_files`) | must |
| 3 | Results can't be forged: the swarm scores every implementor's commit itself with a locked, deterministic-enough command it runs (`eval.full_command`), never an implementor's self-report | must |
| 4 | A holdout exists that implementors cannot reach: sealed in code, gated by an env var the swarm strips (`strip_env`), or stored outside the repo | must |
| 5 | Implementors' turns are independent: separate worktrees suffice (check shared state — databases, caches, fixed ports, files outside the repo) | must |
| 6 | Trials are boundable: the round/agent counts and budget bound how many turns will run, even without an exact per-turn trial count | must |
| 7 | A cheap, tractable proxy eval exists — cheap enough to run after every implementor turn — that correlates well enough with the full score for promotion to mean something | must |
| 8 | Collaboration plausibly adds value over blind parallel search: agents can usefully react to each other's proposals, critiques, and results — not just retry independently with no shared state | must |
| 9 | One implementor turn's evaluation (at least the proxy) costs minutes, not hours | should |
| 10 | The search space is wide: several implementors have real, distinct directions to pursue at once | should |
| 11 | The expected value justifies dozens of rounds across several communicating agents | should |

Write `verdict.md`: a table of criterion, PASS/FAIL, and evidence. Then add exactly one line
(`collab.py init` requires this exact text to proceed):

- `VERDICT: GO`: every must passes. A failed should also passes if the human overrides it; log
  their reason under the table.
- `VERDICT: NOT-A-SWARM`: criterion 1 fails. Say what to use instead: `/ralph-loop` on a plan, or
  `/swarm-loop` if the goal is a search problem but collaboration (criterion 8) wouldn't help. Stop.
- `VERDICT: NOT-YET`: another must fails. Write a draft prerequisites spec in the setup dir,
  `<name>/swarm/<date>-prerequisites.md`, one requirement with
  a checkable definition of done per failed criterion. Point the human to grilling → plan →
  `/ralph-loop` on it. Stop. Fixing a scorer, a holdout, or forge-proofing is the human's reviewed
  work, never done inside this skill.

Show the human the verdict with its evidence. Continue only on GO.

## 2. Roles and eval adapters

Read `roles/strategist.md`, `roles/implementor.md`, `roles/critic.md`, and `roles/retro.md` — they are the
human-reviewed operating contract each role's turn is built from (drop-file schema, message quota,
what to post). Adjust them for this goal's specifics if needed (the target's conventions, its fast
test command, what a good proposal or critique looks like here); they are hash-locked as control
files once a run starts, so get them right before `init`.

**How a turn is built:** `agents.launch_turn` renders the role's file (its `{goal}`, `{agent}`,
`{round}` and `{view}` placeholders filled) as the whole prompt; the one-line
`agents.ROLE_PREAMBLE` is only a fallback when a role file is missing. To see exactly what an agent
was sent, read `<run-dir>/prompts/<role>-<agent>-r<round>.prompt.md` after its turn.

Write the eval commands `swarm.json` will point `eval.proxy_command` and `eval.full_command` at —
either plain shell, or an `adapters/` script referenced as `"$COLLAB_RUN_DIR/adapters/<name>"` (see
`MANIFEST.md`'s "The eval contract" for the exact env vars and the last-stdout-line JSON shape).
Run each once by hand on the baseline commit and check its output matches the contract — a wrong
eval command is a wrong swarm, since it is the only thing that ever decides a score.

## 3. Probe and manifest

- **Probe.** Time one real proxy eval run on the baseline (`/usr/bin/time -l <proxy eval>`). Record
  wall time, CPU time, and peak memory. Use them, plus the budget, to decide implementor headcount
  (`roles.implementor`): more implementors search more directions per round but divide the same
  round-token and time budget between them. There is no per-implementor file-scoping in this skill
  (no themes/`freeze_dirs`, unlike `/swarm-loop`) — every implementor can touch any file outside
  `locked_files`; distinct directions come from the strategist's proposals and the critic's
  dead-end calls, not from manifest-level partitioning. It's fine to jot 2–3 candidate opening
  directions for the strategist's first round as a review aid; they aren't a manifest field.
- **Manifest.** Write `swarm.json` per `MANIFEST.md`. Put every unlock/credential variable the
  target knows in `strip_env`. Put the scorer, eval/proxy harness, and any holdout-access code in
  `locked_files`. Set `budget.max_rounds`/`budget.max_hours` to values you actually intend to be
  bound by (see the budget note above); set `max_usd` and the token caps too, since they're
  validated and will bind once turn-cost accounting is wired up, but don't rely on them today.

**Gate: approval.** Show the human, in one message: the verdict, the role counts with the probe
arithmetic behind them, the budget (flag which caps are live today), the pilot size, the message
quotas, and the locked files. Wait for approval or changes.

## 4. Initialise and launch the pilot

```bash
COLLAB init <setup-dir>            # prints the run dir
COLLAB start <run-dir>
```

`init` refuses without `VERDICT: GO` or with an invalid manifest; fix the setup and retry. Brief
the human: run dir, role counts, pilot size, and these commands:

- `/swarm-collab status <run-dir>` (or `tail -f <run-dir>/orchestrator.log`)
- `/swarm-collab board <run-dir>` to read what the agents are actually saying to each other
- `/swarm-collab stop <run-dir>`
- `/swarm-collab scale <run-dir>` after the pilot report lands
- `/swarm-collab harvest <run-dir>` when the run ends

The orchestrator is detached, and `caffeinate` keeps the Mac awake. It survives this session. It
stops by itself at the end of the pilot, writing `pilot_report.md`, and at the end of the budget
(reliably, at `max_rounds`/`max_hours` today).

## Fixed ground

- Every verdict comes from the locked eval commands the orchestrator itself runs against a
  commit — never from an agent's own report. A `result` entry's claimed `score` on the blackboard
  is informational only; nothing reads it as ground truth.
- The orchestrator never merges an implementor's worktree or branch into anything. `cleanup`
  removes worktrees/branches; it never keeps one as a result. Landing a winning commit is the
  human's decision, using `harvest.md`'s pointer to it.
- Nothing in this skill runs a lockbox or holdout-confirmation command. If your setup needs one,
  it is the human's own step after harvest, outside `collab.py`.
- A halt (a control file or a locked file changed, the blackboard's history was rewritten, an eval
  command broke, the orchestrator crashed) waits for the human. Report its reason; resuming
  (`--clear-halt`) is their call, never this skill's.
