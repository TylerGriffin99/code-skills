# swarm-collab: collaborative multi-agent search — design

Status: draft for review
Date: 2026-09-28
Author: Tyler Griffin (with AI assistance)

## Summary

`swarm-collab` is a new personal skill (`~/.claude/skills/swarm-collab/`) that runs an
open-ended, compute-heavy **build-and-measure search** in which many real `claude -p` agents
**collaborate** — communicating through a bounded shared blackboard and a small quota of direct
messages — to find solutions that no plan can specify in advance.

It is the third member of an existing family:

| Skill | Concurrency | Communication | Driver | Use when |
|---|---|---|---|---|
| `ralph-loop` | 1 lane, sequential | none | written plan + executable DoD | a plan can be written |
| `swarm-loop` | many lanes, parallel | **indirect** (registry + 1-line hypothesis) | locked scorer | no plan; blind parallel search |
| `swarm-collab` (this) | many agents, parallel | **direct** (blackboard + capped DMs) | locked scorer | no plan; search benefits from shared learning/critique |

Target problems: "train a CNN to within x% of a golden dataset", "evolve an agent harness to
99% accuracy" — anything where thousands of candidate solutions must be implemented and tested,
and where agents building on each other's findings (and challenging each other before compute is
spent) is more sample-efficient than blind parallel lanes.

Verdicts always come from a locked, deterministic scorer — never an agent's self-report. This is
the same honesty discipline as `swarm-loop`, and it is non-negotiable.

## Goals

1. Let many real `claude -p` agents collaborate on an unplannable search, in distinct **roles**
   (strategist, implementor, critic) rather than as identical lanes.
2. Make collaboration feel like a "constrained free-for-all" — agents may post freely and message
   named peers — while **guaranteeing in code** that no agent ever blows the token budget or the
   context window.
3. Reuse the *patterns* proven in `swarm.py` (worktree isolation, locked scorer, holdout sealing,
   budgets, ledger, halt-on-tamper, harvest) in a **standalone** orchestrator (`collab.py`) that
   shares no code with `swarm.py`.
4. Keep the human as the only party that merges a lane or runs a lockbox command.
5. Be testable without spending tokens or GPU, via a scripted mock agent.

## Non-goals

- Not a generic, API-agnostic LLM framework. Agents are real `claude -p` processes; there is no
  pluggable "LLM client" abstraction (the pasted hand-off spec's `swarm_skills/` module is
  explicitly rejected — it re-implements the iterative pattern `ralph-loop`/`swarm-loop` already
  own, and a mock LLM cannot train a CNN or run a harness).
- Not a re-implementation of Pattern 1 (iterative refinement). That already exists.
- Not a literal single-thread group chat over the whole swarm. A shared thread with thousands of
  turns serializes the work and blows context; the conversational/debate pattern is used only at
  the small **council** layer, never as the swarm-wide transport.
- Not a merge or deploy tool. `swarm-collab` never merges a lane and never runs a lockbox command.
- v1 does **not** modify `swarm.py` or `swarm-loop`. They stay untouched.

## Definitions

- **Run** — one execution of a configured swarm-collab search, in its own run dir.
- **Agent** — one `claude -p` process with a role, launched for one turn (fresh context each turn,
  exactly like ralph/swarm iterations). Agents have no memory between turns; the blackboard is
  their memory.
- **Role** — strategist, implementor, or critic (see "Roles"). The evaluator is a *command*, not
  an agent.
- **Blackboard** — the append-only, hash-gated shared memory of typed entries (see "Blackboard").
- **View** — the bounded, role-tailored, token-capped slice of the blackboard the orchestrator
  renders for a single agent turn. Agents never read the raw blackboard.
- **Round** — one scheduling cycle: the orchestrator admits a bounded set of agent turns, runs
  implementors in parallel worktrees, scores results, then runs a light council step.
- **Council** — a small end-of-round step where strategist + critic agents read a digest and set
  the next round's directions; may emit an early-stop signal.
- **Setup dir** — `<repo>/docs/swarm-collab/<name>/`: human-reviewable configuration and adapters.
- **Run dir** — `<setup-dir>/<run-id>/`: the locked copy of the setup plus runtime state.

## Architecture

### Component overview

```
                         collab.py (standalone orchestrator, detached)
                         ── owns: run dir, worktrees, budget, scheduler,
                            blackboard, message bus, eval, locking, harvest
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────┐
        │                                  │                                  │
   Strategist(s)                     Implementor(s)                       Critic(s)
   claude -p, few                    claude -p, many                      claude -p, few
   read digest+best →                take one proposal →                  challenge proposals
   post proposals                    build+test in worktree →             BEFORE compute,
                                     post scored result                   flag dead-ends,
        │                                  │                              review diffs
        └──────────────┬───────────────────┴──────────────┬──────────────────┘
                        ▼                                   ▼
                 BLACKBOARD (append-only JSONL, hash-gated)   ◄── Evaluator = locked
                 hypothesis│proposal│result│critique│              scorer COMMAND
                 deadend│message│council│digest                    (not an agent;
                        │                                           sole source of "success")
                        ▼
                 BOUNDED VIEW RENDERER  ── pinned facts + role-relevant, token-capped slice;
                                           summarizer compacts old entries into digests
```

### 1. Orchestrator (`collab.py`)

Standalone, borrowing patterns from `swarm.py` but no shared code. Responsibilities:

- Own the run dir, `config`/state, and the detached lifecycle (`caffeinate`, survives the session,
  pid file, `STOP` file, resumable) — mirroring `swarm.py`'s process model.
- Create and reap git worktrees for implementor turns.
- Own the **blackboard** (append + read), the **view renderer**, the **summarizer**, and the
  **message bus**.
- Run the **scheduler** (rounds, who acts, parallelism caps).
- Run the **evaluator** (locked scorer) and record scores; nondeterministic evals use a fixed seed
  and a `verify_tolerance`, as in `swarm.py`.
- Enforce **budgets** (global `max_usd`, per-round and per-agent token caps).
- Enforce **locking/honesty**: hash-check locked files + blackboard after each round; `strip_env`
  unlock vars from every agent; halt-on-tamper.
- Produce reports: `pilot_report.md`, `harvest.md`.

Management subcommands mirror `swarm.py`: `init`, `start`, `run` (the detached loop), `status`,
`stop`, `scale`, `harvest`, `cleanup`, `relock`, plus a `board` read command for humans.

### 2. Roles

Each role is a persona system-prompt/preamble file (see "Files"), rendered with run/round values
like `swarm-loop`'s `lane_preamble.md`.

- **Strategist / Proposer** (few, e.g. 1–3). Input view: the digest, the current best result(s),
  open critiques, recent proposals. Output: one or more `proposal` entries — a concrete direction
  for implementors to try, each with a rationale and a distinctness note vs. what's been tried.
- **Implementor** (many, the bulk of the budget). Input view: one assigned `proposal` (or picks an
  open one), the locked rules, the target's conventions, its fast test/eval commands. Runs in its
  own worktree. Delegates heavy reading/output to subagents (same pattern as `lane_preamble.md`).
  Makes ONE change + its test, runs the fast/proxy eval, and posts a `result` entry. The
  orchestrator — not the agent — attaches the authoritative score from the locked scorer.
- **Critic / Reviewer** (few). Input view: recent proposals and results. Output: `critique`
  entries (challenge a proposal before compute is spent; predict a dead-end with reasons; review a
  diff for scope/soundness). Critics spend cheap tokens to save expensive implementor tokens.
- **Evaluator** — the locked scorer command, run by the orchestrator. Two-tier (see "Proxy eval").

### 3. Blackboard

Append-only JSONL at `<run-dir>/board.jsonl` (locked/hash-gated; agents write only via the
orchestrator's append API, never by editing the file). Entry types share a common envelope
(`id`, `round`, `role`, `author_agent`, `ts`, `type`, `refs`) plus a typed body:

- `hypothesis` — a question/idea to explore.
- `proposal` — a concrete direction for implementors (from strategists).
- `result` — files changed, test added, proxy score (agent-reported) **and** authoritative score
  (orchestrator-attached from the locked scorer), commit ref.
- `critique` — challenge / dead-end prediction / diff review, `refs` the target entry.
- `deadend` — a confirmed dead direction, so no one retries it.
- `message` — a direct, addressed peer message (the constrained free-for-all; see "Messaging").
- `council` — a round's consolidated decisions and next-round directions; may carry
  `terminate: true` as an early-stop signal.
- `digest` — a summarizer-produced compaction of older entries.

### 4. Bounded context (the key guarantee)

No agent ever receives the raw blackboard. For each turn the orchestrator renders a **view**:

- **Pinned facts** (always, small): the goal, the current best score + how it was achieved, the
  locked rules, the agent's role and assignment.
- **Role-relevant slice** (token-capped): e.g. implementors get their proposal + relevant prior
  results/critiques; strategists get the digest + top-scoring results + open critiques; critics get
  recent proposals/results. Slice selection is by recency + score + relevance to the agent's refs.
- **Addressed messages**: direct messages to this agent (within quota).

A hard **per-view token ceiling** is enforced. When the blackboard's live (un-digested) region
exceeds a threshold, a **summarizer pass** (a cheap `claude -p` call, or a deterministic reducer —
decided in the plan) compacts old entries into `digest` entries and marks the originals archived.
This is the concrete mechanism behind "smoothly manage context window degradation" and the "never
blow context" requirement — it is enforced by the renderer, not by asking agents to self-limit.

### 5. Messaging (constrained free-for-all)

- Agents may send a small **quota** of direct `message` entries per turn (e.g. ≤ N, each ≤ M
  tokens), addressed to named peers/roles.
- Messages are delivered through the next recipient's view (not a live channel — turns are
  discrete, fresh-context).
- Quotas, sizes, and total message budget are orchestrator-enforced and counted against the token
  budget, so chatter can never starve implementation or blow the budget.
- v1 keeps messaging deliberately narrow; if it proves low-value in the pilot it can be disabled by
  config without touching the blackboard/council path.

### 6. Scheduling / rounds

Each round:
1. **Strategist turns** (if due): produce/refresh proposals.
2. **Critic turns** (if due): filter proposals, kill predicted dead-ends before compute.
3. **Implementor turns**: parallel, in worktrees, capped by the probe-derived lane count (cores ÷
   CPU-bound factor, memory, and budget), each posts a scored `result`.
4. **Council step**: strategists + critics read the digest, consolidate, set next-round directions,
   possibly emit `terminate`.

Lane-count arithmetic and the probe (`/usr/bin/time -l`) mirror `swarm-loop` step 3.

### 7. Proxy eval (two-tier evaluation)

Because a real eval may take hours (full CNN training), the setup **must** provide:

- a fast **proxy eval** (`proxy_eval_command`) — small data subset / few epochs / reduced harness —
  for the inner loop, run on every implementor result; and
- the full **scorer** (`eval_command` / `verify_command`) — run only to **promote** a candidate
  that clears a proxy threshold, and at harvest.

The fitness review fails a goal that has no tractable proxy. Both commands are in `locked_files`.

### 8. Budget & termination

- Global `max_usd` (mandatory), plus per-round and per-agent token caps — all orchestrator-enforced
  (patterned on `swarm.py`'s budget accounting).
- Terminate on any of: target score reached (from the scorer), budget exhausted, stall (no
  improvement over N rounds), council `terminate` early-stop, or human `stop`.
- A council `terminate` may end the run early but **never** declares success on its own — success is
  the scorer clearing the target on the full eval + holdout.

### 9. Honesty & safety (house rules, unchanged from swarm-loop)

- Scores come only from the locked scorer / `verify` command, never an agent's self-report.
- `locked_files`: scorer, proxy eval, full eval/harness, holdout access code, role preambles.
- `strip_env`: every unlock/credential var the target knows, stripped from all agent environments.
- Holdout unreachable by agents (sealed in code / env-gated / stored outside the repo).
- Hash-check locked files **and** the blackboard after each round; any change quarantines the
  offending lane and halts the run for the human.
- The swarm never merges a lane and never runs a lockbox command — both are the human's.

## Skill surface (`SKILL.md`)

`disable-model-invocation: true`. Argument `$ARGUMENTS`.

Management commands (run and report, nothing else): `status | stop | resume | scale | harvest |
cleanup | board <run-dir>`, plus a control-files-changed → `relock` path — mirroring
`swarm-loop`'s management block.

Otherwise the argument is a goal, and the skill runs:

1. **Fitness review** → `verdict.md` (GO / NOT-A-SWARM / NOT-YET). Criteria = `swarm-loop`'s
   must/should set **plus**: (a) collaboration adds value over blind parallelism (evidence, not
   impression); (b) a tractable proxy eval exists. NOT-A-SWARM points to `/ralph-loop` or
   `/swarm-loop`; NOT-YET writes a prerequisites spec, as `swarm-loop` does.
2. **Roles & adapters** → write the role preambles, `proxy_eval`/`eval`/`verify` adapters; run each
   adapter once on the baseline and check its JSON against the contract in `MANIFEST.md`.
3. **Themes/directions, probe, manifest** → probe one real proxy eval for timing/memory; compute
   lane count; write `swarm.json` (manifest) with locked files, strip_env, budgets, round schedule,
   message quotas, proxy/full thresholds.
4. **Gate: approval** → show the human verdict, roles, lane count + arithmetic, budget, thresholds,
   locked files. Continue only on approval.
5. **Init & launch pilot** → `collab.py init` then `start`; brief the human on watch/stop/scale
   commands. Detached; survives the session; stops itself at end of pilot and at budget.

Layout: setup dir `<repo>/docs/swarm-collab/<name>/`; run dir inside it. Reports committed by the
human; locked copy + runtime state gitignored. Nothing under `~/.claude` except the skill itself.

## Testing

The pasted hand-off spec's "mock LLM CLI" is kept **only** as a test harness, not as the product:

- **Mock agent** — a scripted fake `claude -p` that emits predetermined blackboard entries for a
  role/round, so tests spend no tokens and no GPU.
- **Unit tests**: blackboard append/read + hash-gating; view renderer (respects the token ceiling,
  role slicing, addressed-message delivery); summarizer/compaction (old entries become digests, live
  region stays under threshold); budget accounting (per-agent/per-round/global caps, message budget);
  locking/halt-on-tamper; scheduler (round order, parallelism cap, termination conditions).
- **End-to-end smoke test**: a tiny toy problem (e.g. drive a number toward a target via a trivial
  locked scorer) run with mock agents, asserting the run reaches the target, respects the budget,
  and halts on a simulated locked-file tamper.

Manual/optional: one real low-budget pilot on a small target, gated by the human.

## Files

```
~/.claude/skills/swarm-collab/
├── SKILL.md                 # the skill (management commands + goal workflow)
├── collab.py                # standalone orchestrator
├── MANIFEST.md              # manifest schema, blackboard/role/adapter contracts, worked examples
├── roles/
│   ├── strategist.md        # persona preamble
│   ├── implementor.md       # persona preamble (patterned on swarm-loop lane_preamble.md)
│   └── critic.md            # persona preamble
└── tests/
    ├── mock_agent.py        # scripted fake claude -p
    ├── test_blackboard.py
    ├── test_view_budget.py  # view ceiling + summarizer/compaction + budget accounting
    ├── test_locking.py
    ├── test_scheduler.py
    └── test_e2e_smoke.py
```

## Risks & open questions (for the plan)

- **Summarizer cost/quality.** LLM summarizer (better digests, costs tokens) vs. deterministic
  reducer (free, dumber). Decide in the plan; possibly configurable.
- **Collaboration ROI.** Direct messaging may add chatter without payoff. Mitigation: it's capped
  and config-disablable; the pilot report should measure whether messaged/critiqued proposals
  outscore un-critiqued ones.
- **Proxy↔full correlation.** A proxy that doesn't correlate with the full scorer wastes compute.
  The setup should sanity-check correlation on the baseline; flag as a fitness concern.
- **Duplicated plumbing vs. swarm.py.** The standalone choice means worktree/eval/budget/locking
  logic is re-implemented; the plan should copy `swarm.py`'s shapes closely to limit drift.
- **Long full evals blocking a worktree.** Full-eval promotions may need their own scheduling lane
  so they don't stall implementor throughput.

## Alternatives considered

- **Generic `swarm_skills/` framework with a mock LLM (the hand-off spec, verbatim).** Rejected:
  re-implements Pattern 1 you already have, and a mock LLM cannot do the real build-and-measure job.
- **Extend `swarm.py` in place / import it as a library.** Rejected by the user in favor of a
  standalone orchestrator (cleaner separation; accepted cost is duplicated plumbing).
- **Literal swarm-wide group chat (Pattern 2 as the transport).** Rejected: serializes work and
  blows context at scale. Kept only for the small council layer.
