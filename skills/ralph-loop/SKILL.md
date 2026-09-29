---
name: ralph-loop
description: Start (or resume) a fresh-context ralph loop on a superpowers spec or plan, gated by an executable definition of done. Usage /ralph-loop <spec-or-plan-path> | resume <run-dir>
disable-model-invocation: true
---

# ralph-loop

Set up a **ralph loop** and launch it: `ralph-loop.sh` runs `claude -p` again and again, each
iteration with fresh context, doing one plan task per iteration, until `dod.sh` is **green**
and the reviews are written, or the loop is **blocked**.

This session does the setup and the human gates. The loop does the implementation. Everything
lives next to this file: `dod.sh`, `ralph-loop.sh`, `iteration.md` (the per-iteration prompt).
Call `SKILL_DIR` the directory containing this file.

Argument: `$ARGUMENTS`

## 0. Resume branch

If the argument is `resume <run-dir>`: read `<run-dir>/runner.log` (tail) and the worktree's
`.ralph/BLOCKED.md` if present. Show the human why it stopped. If it was blocked, help them
resolve it (they decide), then delete `BLOCKED.md` and relaunch as in step 6. Stop here.

## 1. Resolve spec and plan

Classify the path:

- **Plan**: contains `REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development` and
  `### Task` headings (usually `docs/superpowers/plans/`). Find its spec: a path the plan
  names, else a file in `docs/superpowers/specs/` on the same topic. If none exists, ask the
  human for one or confirm running spec-less (the plan's Goal becomes the reference).
- **Spec**: anything else (usually `docs/superpowers/specs/*-design.md`). Find a plan in
  `docs/superpowers/plans/` built from it. If none exists, invoke `superpowers:writing-plans`
  on the spec to write one.

**Gate: plan approval.** Show the human the plan's task list (titles only) and ask them to
approve it before continuing. The plan is the loop's whole script; this is the cheapest place
to fix direction.

Done when: one spec path (or explicit spec-less), one approved plan path.

## 2. Draft the checks file

Turn the spec's definition of done (acceptance criteria, success criteria, "done when") into a
checks file, `docs/superpowers/dod/<plan-basename>.checks`:

    # DoD for <plan>. Source: <spec path>
    @base <base branch>
    <name> :: <command that exits 0 only when the criterion holds>

Rules for each check:
- One criterion, one check, named for the criterion. Every criterion that a command can decide
  becomes a check; the rest go into a `# MANUAL:` comment block for the human's final review.
- Commands are the narrowest that prove the criterion (specific test files, specific backtest
  assertions), plus the repo's standard gate (for this repo: `make pre-commit`). Read
  `AGENTS.md` / `CONTRIBUTING.md` for the project's gate commands and runner (e.g. `uv run`).
- A test that does not exist yet is still a valid check: the plan creates it.
- `@allow-test-edits <path>` only where the spec deliberately changes existing tested behaviour.
  Name each one to the human.

Base branch: `main` unless the repo's `develop` exists and the human is contributing upstream.
Run `bash SKILL_DIR/dod.sh --help` for the full format.

**Gate: red check.** Run `SKILL_DIR/dod.sh <checks>` on the current branch. Expect **red**, for
the right reasons: failures should be "test not found / assertion failed", never "command not
found", syntax errors, or a check that passes before any work (a check green on day one proves
nothing). Fix the checks until the red is honest. Then show the human the checks file and the
MANUAL list and get approval.

Done when: the checks file is approved and red for honest reasons.

## 3. Commit the inputs

The worktree branches from a commit, so spec, plan, and checks must be committed. If any of
them is uncommitted, tell the human and commit just those files on the current branch (their
approval in step 2 covers it; ask if the current branch is `main`/`develop`).

## 4. Create the worktree and run dir

With `<slug>` = plan basename without date and extension, and `<repo>` = repo directory name:

```bash
git worktree add ../<repo>-ralph-<slug> -b ralph/<slug> <base>
```

If `<base>` is not where the inputs were committed, branch from the current HEAD instead.
Then in the worktree:
- Copy the main checkout's untracked `.claude/` (hooks, settings) into the worktree if
  `git ls-files .claude` shows it is untracked, so the loop runs under the same hooks.
- Append `.ralph/` and (if copied) `.claude/` to `$(git rev-parse --git-common-dir)/info/exclude`.
- Create `.ralph/state.md`:

```markdown
# Ralph loop state
gap_rounds: 0
last_drift_check_at: 0

## Task failures

## Drift checks

## Log
```

Run dir: `~/.claude/ralph/<repo>/<slug>/` (outside the worktree, so the loop cannot rewrite
it). Write `config.env` with `printf 'KEY=%q\n'` lines for: `WORKTREE`, `SPEC` (empty if
spec-less), `PLAN`, `CHECKS` (absolute paths inside the worktree), `BASE`, and optionally
`MAX_ITER` (default 40), `PERMISSION_MODE` (default `auto`), `ITER_BUDGET_USD`, `MODEL`.

Lock the inputs, in this order:
1. Append `@locks ~/.claude/ralph/<repo>/<slug>/locks.sha256` (expanded to an absolute path)
   to the worktree's checks file and commit it on `ralph/<slug>`.
2. Write the manifest over absolute paths of the spec (if any), that checks file, and
   `SKILL_DIR/{dod.sh,iteration.md,ralph-loop.sh}`:

```bash
shasum -a 256 <files...> > ~/.claude/ralph/<repo>/<slug>/locks.sha256
```

Done when: `SKILL_DIR/dod.sh <worktree checks>` run inside the worktree shows
`locks-intact` PASS and the same honest red as step 2.

## 5. Brief the human

Before launching, tell them in a few lines: the branch and worktree, the iteration cap, that
the loop commits locally on `ralph/<slug>` (never pushes), and these commands:

- watch: `tail -f ~/.claude/ralph/<repo>/<slug>/runner.log`
- stop after the current iteration: `touch ~/.claude/ralph/<repo>/<slug>/STOP`
- resume: `/ralph-loop resume ~/.claude/ralph/<repo>/<slug>`
- run it from a terminal instead: `SKILL_DIR/ralph-loop.sh ~/.claude/ralph/<repo>/<slug>`

## 6. Launch

Run `SKILL_DIR/ralph-loop.sh <run-dir>` with Bash `run_in_background: true`. When it exits,
report by exit code: 0 → summarise `.ralph/REVIEW.md` (CRITICAL findings first) and the MANUAL
list; 3/4 → show `BLOCKED.md`; 5 → show which locked file changed and the diff; others → the
tail of `runner.log`. Merging is the human's call.
