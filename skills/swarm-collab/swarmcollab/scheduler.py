"""Round/council scheduler and termination: the integration keystone.

Ties together board, views, agents, evaluate, locks, and budget into one round
of collaboration: strategist turns, critic turns, parallel implementor turns
(each scored via the locked eval), and an occasional council vote — followed
by advancing the board's locked prefix and compacting the view if needed.

`termination_reason` is the *between-round* gate: it re-checks every lock
independently (control files, target files, board append-only), then target
score, budget, stall, council vote, and the STOP file, in that priority
order. It is meant to be called by the orchestrator loop after each
`run_round`, never from inside `run_round` itself — see the note on
`_board_locked_out` below for why `run_round` does not call
`locks.check_control_locks` directly.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from swarmcollab import agents, board, budget, evaluate, locks, util, views
from swarmcollab.util import CollabError

DEFAULT_ITERATION_TIMEOUT_MIN = 120
DEFAULT_STALL_ROUNDS = 3


# --------------------------------------------------------------------------
# worktrees
# --------------------------------------------------------------------------

def make_worktree(r, agent: str, start_commit: str) -> Path:
    """Create (or reuse) `agent`'s persistent worktree on `collab/{run_id}/{agent}`.

    Mirrors swarm.py's `create_lane` worktree setup. `.collab/` is protected
    from the target's own cleanup (e.g. `git clean -fd`) via the repo's
    `info/exclude`, the same mechanism swarm.py uses for `.swarm/`.
    """
    branch = f"collab/{r.run_id}/{agent}"
    worktree = r.lanes_root / agent
    r.lanes_root.mkdir(parents=True, exist_ok=True)

    if worktree.exists():
        # A leftover from an earlier round/run: drop it so branch state is clean.
        util.run(["git", "-C", str(r.repo), "worktree", "remove", "--force", str(worktree)], check=False)

    branch_exists = util.run(
        ["git", "-C", str(r.repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    ).returncode == 0
    if branch_exists:
        util.git(r.repo, "worktree", "add", "-q", "-f", str(worktree), branch)
        util.git(worktree, "reset", "--hard", start_commit)
    else:
        util.git(r.repo, "worktree", "add", "-q", "-b", branch, str(worktree), start_commit)

    common_dir = Path(util.git(r.repo, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = r.repo / common_dir
    exclude = common_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text().splitlines() if exclude.exists() else []
    if ".collab/" not in existing:
        with open(exclude, "a") as fh:
            fh.write(".collab/\n")

    return worktree


def reap_worktree(r, path) -> None:
    """Remove a worktree created by `make_worktree`. Never raises: best-effort cleanup.

    A failed removal is still best-effort (never raised), but no longer silent: it
    logs a `worktree_reap_failed` event so a worktree left behind — one that a later
    unchecked force-remove could discard along with a real lock violation — is visible.
    """
    proc = util.run(["git", "-C", str(r.repo), "worktree", "remove", "--force", str(path)], check=False)
    if proc.returncode != 0:
        r.event("worktree_reap_failed", f"git worktree remove failed for {path}: {proc.stderr.strip()[-300:]}")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _drop_dir(r) -> Path:
    return r.dir / "drops"


def _drop_path(drop_dir, role: str, agent: str, round_no: int) -> Path:
    return Path(drop_dir) / f"{role}-{agent}-r{round_no}.jsonl"


def _model_for(r, role: str) -> str:
    models = r.m.get("models")
    if isinstance(models, dict) and models.get(role):
        return models[role]
    return r.m.get("model", "default")


def _iteration_timeout_sec(r) -> float:
    return float(r.m.get("iteration_timeout_min", DEFAULT_ITERATION_TIMEOUT_MIN)) * 60


def _role_count(r, role: str, default: int = 0) -> int:
    return int((r.m.get("roles") or {}).get(role, default) or default)


def _wait_for(proc, timeout_sec: float) -> None:
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        proc.wait()


def _poll_parallel(r, infos: list, timeout_sec: float) -> None:
    """Poll every info['proc'] to completion, killing and flagging any that exceed `timeout_sec`."""
    remaining = list(infos)
    while remaining:
        still = []
        for info in remaining:
            proc = info["proc"]
            if proc.poll() is not None:
                continue
            if time.time() - info["started"] >= timeout_sec:
                # Killing/reaping a timed-out process is best-effort — execution
                # correctly continues either way — but a failure is logged rather
                # than silently swallowed.
                try:
                    proc.kill()
                except Exception as exc:
                    r.event("poll_kill_failed", f"kill failed for {info['agent']}: {exc}", info["agent"])
                try:
                    proc.wait(timeout=5)
                except Exception as exc:
                    r.event("poll_wait_failed", f"wait after kill failed for {info['agent']}: {exc}", info["agent"])
                info["timed_out"] = True
                continue
            still.append(info)
        remaining = still
        if remaining:
            time.sleep(0.2)


def _latest_turn_id(r, round_no: int, role: str, agent: str):
    row = r.db.execute(
        "SELECT id FROM turns WHERE round=? AND role=? AND agent=? ORDER BY id DESC LIMIT 1",
        (round_no, role, agent),
    ).fetchone()
    return row["id"] if row else None


def _board_locked_out(r) -> bool:
    """True (and records a persisted halt) if the board's locked prefix was rewritten.

    This is the only lock `run_round` checks internally between sub-steps.
    `check_control_locks` is deliberately NOT checked here: control-file
    tampering is a between-round concern that `termination_reason` (called by
    the orchestrator after `run_round` returns) already re-checks fresh and
    independently. Checking it mid-round as well would abandon a round's
    already-scripted sub-steps over a manifest edit that happened before the
    round even started, with no way to "undo" the turns already launched.

    Fails CLOSED: if `locks.json` hasn't been written yet, `check_board_append_only`
    raises `CollabError`, and that propagates rather than being swallowed here.
    "Cannot tell if the board was tampered with" must never be treated as
    "not tampered" — the same reasoning `termination_reason`'s own direct
    lock calls already follow. Normal flow always calls `locks.write_locks`
    before the first round, so this is not expected to raise in practice.
    """
    reason = locks.check_board_append_only(r)
    if reason:
        r.set("halt", reason)
        r.event("halt", reason)
        return True
    return False


# --------------------------------------------------------------------------
# round sub-steps
# --------------------------------------------------------------------------

def _run_role_turns(r, role: str, round_no: int, launch, drop_dir: Path) -> None:
    count = _role_count(r, role, default=0)
    if count <= 0:
        return
    model = _model_for(r, role)
    timeout_sec = _iteration_timeout_sec(r)
    for i in range(1, count + 1):
        # Enforce the token caps: once this round's spend hits round_token_cap,
        # launch no further turns; skip any single agent already over its cap.
        cap_reason = budget.round_cap_reason(r, round_no)
        if cap_reason:
            r.event("round_cap_reached", cap_reason)
            break
        agent = f"{role}-{i}"
        if budget.agent_cap_exceeded(r, agent, round_no):
            r.event("agent_cap_reached", f"{agent} round {round_no} at/over agent_token_cap; skipped", agent)
            continue
        proc = launch(r, role=role, agent=agent, model=model, round=round_no, cwd=r.repo, drop_dir=drop_dir)
        _wait_for(proc, timeout_sec)
        turn_id = _latest_turn_id(r, round_no, role, agent)
        # reap + ingest sit together so one turn's bad drop can never skip the
        # reap (or abort the sibling turns still queued behind it).
        try:
            agents.reap_turn(r, role=role, agent=agent, round=round_no, turn_id=turn_id,
                             returncode=proc.returncode)
            drop_file = _drop_path(drop_dir, role, agent, round_no)
            agents.ingest_drop(r, role=role, agent=agent, round=round_no, drop_file=drop_file)
        except CollabError as exc:
            reason = f"{role} {agent} round {round_no} turn failed: {exc}"
            r.event("turn_failed", reason, agent)
            if turn_id is not None:
                r.db.execute("UPDATE turns SET is_error=1, note=? WHERE id=?", (str(exc)[:2000], turn_id))
                r.db.commit()


def _run_implementor_turns(r, round_no: int, launch, drop_dir: Path) -> bool:
    """Run every implementor's turn in a parallel worktree, then score each.

    Returns True if a target-lock violation was found in any worktree (the
    caller halts the round early in that case).
    """
    count = _role_count(r, "implementor", default=1)
    model = _model_for(r, "implementor")
    timeout_sec = _iteration_timeout_sec(r)
    start_commit = r.m["baseline_commit"]

    infos = []
    for i in range(1, count + 1):
        # Stop launching further implementors this round once the round token cap
        # is hit; skip any single implementor already over its per-agent cap.
        cap_reason = budget.round_cap_reason(r, round_no)
        if cap_reason:
            r.event("round_cap_reached", cap_reason)
            break
        agent = f"implementor-{i}"
        if budget.agent_cap_exceeded(r, agent, round_no):
            r.event("agent_cap_reached", f"{agent} round {round_no} at/over agent_token_cap; skipped", agent)
            continue
        expected_worktree = r.lanes_root / agent
        try:
            worktree = make_worktree(r, agent, start_commit)
            proc = launch(r, role="implementor", agent=agent, model=model, round=round_no,
                          cwd=worktree, drop_dir=drop_dir)
        except Exception as exc:
            # make_worktree/launch failed before this implementor has any tracked
            # process: log it and move on rather than aborting every implementor
            # still queued behind it. Best-effort reap in case make_worktree got
            # partway through (e.g. `worktree add` succeeded but the launch
            # subprocess then failed to start).
            reason = f"failed to launch implementor {agent} round {round_no}: {exc}"
            r.event("turn_launch_failed", reason, agent)
            reap_worktree(r, expected_worktree)
            continue
        infos.append({"agent": agent, "worktree": worktree, "proc": proc,
                      "started": time.time(), "timed_out": False})

    _poll_parallel(r, infos, timeout_sec)

    violated = False
    for info in infos:
        agent = info["agent"]
        worktree = info["worktree"]
        turn_id = _latest_turn_id(r, round_no, "implementor", agent)

        # Everything past this point (reap, drop ingestion, target-lock check,
        # `git rev-parse`, and scoring) touches the worktree, the drop file, and/or
        # runs the locked eval commands, any of which can raise. The worktree must
        # ALWAYS be reaped for this implementor regardless of how it exits, and a
        # failure here must not abort the implementors still left to process — so
        # `ingest_drop` lives INSIDE this try/finally alongside scoring.
        try:
            agents.reap_turn(r, role="implementor", agent=agent, round=round_no, turn_id=turn_id,
                             returncode=info["proc"].returncode, timed_out=info["timed_out"])
            drop_file = _drop_path(drop_dir, "implementor", agent, round_no)
            posted = agents.ingest_drop(r, role="implementor", agent=agent, round=round_no, drop_file=drop_file)

            if info["timed_out"]:
                r.event("turn_timed_out", f"implementor {agent} round {round_no} exceeded timeout", agent)
                continue

            bad_target = locks.check_target_locks(r, worktree)
            if bad_target:
                reason = f"locked files changed in {agent}'s worktree: {', '.join(bad_target)}"
                r.set("halt", reason)
                r.event("target_lock_violation", reason, agent)
                violated = True
                continue

            # Link the swarm's authoritative score back to the agent's own board
            # `result` entry so shared views can rank/show the eval's number
            # instead of the agent's self-report (see views._authoritative_scores).
            result_entry = next((e for e in reversed(posted) if e["type"] == "result"), None)
            board_entry_id = result_entry["id"] if result_entry else None

            commit = util.git(worktree, "rev-parse", "HEAD")
            evaluate.score_result(r, worktree=worktree, commit=commit, agent=agent,
                                  round=round_no, turn_id=turn_id, board_entry_id=board_entry_id)
        except CollabError as exc:
            reason = f"implementor {agent} round {round_no} turn failed: {exc}"
            r.event("turn_failed", reason, agent)
            if turn_id is not None:
                r.db.execute(
                    "UPDATE turns SET is_error=1, note=? WHERE id=?",
                    (str(exc)[:2000], turn_id),
                )
                r.db.commit()
        finally:
            reap_worktree(r, worktree)

    return violated


def council(r, round_no: int, *, launch) -> bool:
    """Run a strategist and a critic council turn reading the current digest.

    Both turns render the normal role view (which already surfaces the
    latest digest entry, if any) so no separate "digest read" step is
    needed. Returns True iff either turn's drop posted a `council` entry
    with `body["terminate"] is True`.
    """
    drop_dir = _drop_dir(r)
    timeout_sec = _iteration_timeout_sec(r)
    terminate = False
    for role in ("strategist", "critic"):
        agent = f"council-{role}"
        proc = launch(r, role=role, agent=agent, model=_model_for(r, role), round=round_no,
                      cwd=r.repo, drop_dir=drop_dir)
        _wait_for(proc, timeout_sec)
        turn_id = _latest_turn_id(r, round_no, role, agent)
        agents.reap_turn(r, role=role, agent=agent, round=round_no, turn_id=turn_id,
                         returncode=proc.returncode)
        drop_file = _drop_path(drop_dir, role, agent, round_no)
        posted = agents.ingest_drop(r, role=role, agent=agent, round=round_no, drop_file=drop_file)
        for entry in posted:
            body = entry.get("body")
            if entry["type"] == "council" and isinstance(body, dict) and body.get("terminate") is True:
                terminate = True
    return terminate


# --------------------------------------------------------------------------
# round driver
# --------------------------------------------------------------------------

def run_round(r, round_no: int, *, launch=agents.launch_turn) -> None:
    rounds_cfg = r.m.get("rounds") or {}
    drop_dir = _drop_dir(r)
    drop_dir.mkdir(parents=True, exist_ok=True)

    if _board_locked_out(r):
        return

    if round_no % int(rounds_cfg.get("strategist_every", 1)) == 0:
        _run_role_turns(r, "strategist", round_no, launch, drop_dir)
        if _board_locked_out(r):
            return

    if round_no % int(rounds_cfg.get("critic_every", 1)) == 0:
        _run_role_turns(r, "critic", round_no, launch, drop_dir)
        if _board_locked_out(r):
            return

    violated = _run_implementor_turns(r, round_no, launch, drop_dir)
    if violated or _board_locked_out(r):
        return

    if round_no % int(rounds_cfg.get("council_every", 1)) == 0:
        council(r, round_no, launch=launch)
        if _board_locked_out(r):
            return

    locks.advance_board_prefix(r)
    if views.needs_compaction(r):
        views.compact(r)


# --------------------------------------------------------------------------
# termination
# --------------------------------------------------------------------------

def _best_full_score(r):
    maximize = bool(r.m.get("maximize", True))
    agg = "MAX" if maximize else "MIN"
    row = r.db.execute(f"SELECT {agg}(full_score) AS best FROM results WHERE full_score IS NOT NULL").fetchone()
    return row["best"] if row is not None else None


def _best_full_score_through(r, round_no: int):
    maximize = bool(r.m.get("maximize", True))
    agg = "MAX" if maximize else "MIN"
    row = r.db.execute(
        f"SELECT {agg}(full_score) AS best FROM results WHERE full_score IS NOT NULL AND round<=?",
        (round_no,),
    ).fetchone()
    return row["best"] if row is not None else None


def _best_proxy_score(r):
    maximize = bool(r.m.get("maximize", True))
    agg = "MAX" if maximize else "MIN"
    row = r.db.execute(f"SELECT {agg}(proxy_score) AS best FROM results WHERE proxy_score IS NOT NULL").fetchone()
    return row["best"] if row is not None else None


def _best_proxy_score_through(r, round_no: int):
    maximize = bool(r.m.get("maximize", True))
    agg = "MAX" if maximize else "MIN"
    row = r.db.execute(
        f"SELECT {agg}(proxy_score) AS best FROM results WHERE proxy_score IS NOT NULL AND round<=?",
        (round_no,),
    ).fetchone()
    return row["best"] if row is not None else None


def _best_score_through(r, round_no: int):
    """Best score through `round_no`, preferring the authoritative full_score and
    falling back to the cheaper proxy_score only when nothing has been promoted yet.

    This couples target-reached / stall detection to the proxy tier ONLY in the
    absence of any full_score — e.g. a `promote_threshold` set high and never
    cleared, where otherwise only max_rounds/max_hours could ever stop the run.
    Once a full_score exists it wins, so the authoritative tier always governs
    when it is available.
    """
    full = _best_full_score_through(r, round_no)
    if full is not None:
        return full
    return _best_proxy_score_through(r, round_no)


def _stalled(r, round_no: int) -> bool:
    """True if the best score hasn't improved in the last `stall_rounds` rounds.

    Compares the best score recorded through `round_no - stall_rounds` against the
    best recorded through `round_no`; if they're equal (nothing better landed in
    the window), that's a stall. "Best" prefers the authoritative full_score and
    falls back to proxy_score only when no full_score exists yet (see
    `_best_score_through`). Absent any score history on either side of the window,
    this can't yet judge a stall, so it returns False rather than guessing.
    """
    stall_rounds = int((r.m.get("rounds") or {}).get("stall_rounds", DEFAULT_STALL_ROUNDS))
    if round_no < stall_rounds:
        return False
    best_now = _best_score_through(r, round_no)
    if best_now is None:
        return False
    best_before = _best_score_through(r, round_no - stall_rounds)
    if best_before is None:
        return False
    return best_now == best_before


def _council_terminate(r) -> bool:
    entries = board.read(r, types=["council"])
    if not entries:
        return False
    body = entries[-1].get("body")
    return isinstance(body, dict) and body.get("terminate") is True


def termination_reason(r, round_no: int) -> str | None:
    bad_control = locks.check_control_locks(r)
    if bad_control:
        return "HALT: control files changed: " + ", ".join(bad_control)

    board_reason = locks.check_board_append_only(r)
    if board_reason:
        return f"HALT: {board_reason}"

    halt = r.get("halt")
    if halt:
        return f"HALT: {halt}"

    target = r.m.get("target_score")
    if target is not None:
        # Prefer the authoritative full_score; fall back to the best proxy_score
        # only when nothing has been promoted yet, so a set-and-never-cleared
        # promote_threshold can't leave target-reached unable to ever fire. The
        # proxy is the cheaper, less authoritative tier — noted in the message.
        best = _best_full_score(r)
        proxy_fallback = best is None
        if best is None:
            best = _best_proxy_score(r)
        if best is not None:
            maximize = bool(r.m.get("maximize", True))
            cleared = best >= target if maximize else best <= target
            if cleared:
                tier = " (proxy)" if proxy_fallback else ""
                return f"target score reached: {best:g} (target {target:g}){tier}"

    reason = budget.budget_reason(r, round_no)
    if reason:
        return reason

    if _stalled(r, round_no):
        stall_rounds = int((r.m.get("rounds") or {}).get("stall_rounds", DEFAULT_STALL_ROUNDS))
        return f"stall: no score improvement over {stall_rounds} rounds"

    if _council_terminate(r):
        return "council voted to terminate"

    if (r.dir / "STOP").exists():
        return "stop file present"

    return None
