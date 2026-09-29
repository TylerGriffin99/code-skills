"""collab.py - orchestrator CLI for /swarm-collab.

Layout, inside the reviewed setup dir (created by the skill, outside this package):
  <setup-dir>/verdict.md, swarm.json, adapters/     reviewed setup
  <setup-dir>/<run-id>/                             one run: pilot_report.md and harvest.md are
                                                     the artifacts meant to be kept; everything
                                                     else (ledger, locks, logs, worktrees) is
                                                     local runtime state

Commands:
  init <setup-dir>                  validate a reviewed setup dir and create a run dir inside it,
                                    with the ledger and lock manifest
  start <run-dir> [--clear-halt]    launch the orchestrator detached (caffeinate + new session);
                                    a halted swarm needs --clear-halt, which is a human decision
  run <run-dir> [--clear-halt]      the orchestrator loop itself (what `start` launches)
  status <run-dir>                  running state, budget totals, best score, recent events
  stop <run-dir> [--now]            finish the current round then exit; --now signals the
                                    orchestrator process directly
  resume <run-dir>                  clear a halt and relaunch detached (an explicit human decision)
  scale <run-dir> [--max-usd X] [--max-rounds N] [--max-hours H]
                                    override budget caps for the rest of the run
  harvest <run-dir>                 write harvest.md: best scores, per-role spend, and the
                                    collaboration-ROI note (never runs a lockbox command)
  cleanup <run-dir> [--confirm]     remove this run's implementor worktrees/branches (dry run by
                                    default); the orchestrator never merges a lane, so nothing here
                                    is ever kept as a result — only harvest.md's report is
  board <run-dir> [--limit N]       print the most recent blackboard entries
  relock <run-dir>                  accept intentional changes to control files (skill upgrade,
                                    adapter fix); a human decision, logged

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from swarmcollab import board, budget, locks, reports, scheduler, util
from swarmcollab.manifest import validate_manifest
from swarmcollab.model import Run
from swarmcollab.util import CollabError, now

SKILL_DIR = Path(__file__).resolve().parent.parent
COLLAB_ENTRY = SKILL_DIR / "collab.py"


# ---------------------------------------------------------------------------------------------
# small helpers shared across commands


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid(pid_file: Path) -> int:
    if not pid_file.is_file():
        return 0
    try:
        return int(pid_file.read_text().strip() or 0)
    except ValueError:
        print(f"collab.py: warning: {pid_file} is corrupt (not a pid); treating as not running",
              file=sys.stderr)
        return 0


def _running_pid(r) -> int:
    pid = _read_pid(r.dir / "orchestrator.pid")
    return pid if _pid_alive(pid) else 0


def _refuse_if_running(r, hint: str) -> None:
    pid = _running_pid(r)
    if pid:
        raise CollabError(f"orchestrator is running (pid {pid}); {hint}")


def _launch_detached(r, *, clear_halt: bool) -> int:
    pid_file = r.dir / "orchestrator.pid"
    pid = _running_pid(r)
    if pid:
        raise CollabError(f"already running (pid {pid})")
    halt = r.get("halt")
    if halt and not clear_halt:
        raise CollabError(
            f"swarm is halted: {halt}\nResolve it, then relaunch with --clear-halt (a human decision)."
        )
    # `_running_pid` above already proved any existing pid file is stale (its pid isn't
    # alive — e.g. a hard-killed prior orchestrator that never reached its `finally`
    # cleanup). Remove it now so the startup poll below can only see the NEW process's
    # pid file appear, instead of finding the old file already present and reporting its
    # dead pid as if the new orchestrator had just started.
    pid_file.unlink(missing_ok=True)
    cmd = [sys.executable, str(COLLAB_ENTRY), "run", str(r.dir)]
    if clear_halt:
        cmd.append("--clear-halt")
    if shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i", *cmd]
    with open(r.dir / "orchestrator.out", "a") as out:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                          start_new_session=True)
    for _ in range(50):
        if pid_file.exists():
            break
        time.sleep(0.2)
    if not pid_file.exists():
        raise CollabError(f"orchestrator did not start; see {r.dir / 'orchestrator.out'}")
    print(f"started pid {pid_file.read_text().strip()}  log: {r.dir / 'orchestrator.log'}")
    return 0


# ---------------------------------------------------------------------------------------------
# init


def cmd_init(args) -> int:
    draft = Path(args.setup_dir).expanduser().resolve()
    verdict = draft / "verdict.md"
    if not verdict.is_file():
        raise CollabError(f"{verdict} missing: run the fitness review first")
    if not re.search(r"^VERDICT:\s*GO\b", verdict.read_text(), re.MULTILINE):
        raise CollabError("verdict.md does not contain 'VERDICT: GO'; swarm-collab only launches on GO")

    manifest_path = draft / "swarm.json"
    if not manifest_path.is_file():
        raise CollabError(f"{manifest_path} missing")
    m = json.loads(manifest_path.read_text())
    errors = validate_manifest(m, draft)
    if errors:
        raise CollabError("manifest invalid:\n  " + "\n  ".join(errors))

    run_id = f"{re.sub(r'[^A-Za-z0-9_-]', '-', m['name'])}-{dt.datetime.now():%Y%m%d-%H%M%S}"
    run_dir = draft / run_id
    if run_dir.exists():
        raise CollabError(f"run dir already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    (run_dir / "logs").mkdir()
    (run_dir / "drops").mkdir()
    (run_dir / "prompts").mkdir()
    (run_dir / ".gitignore").write_text(
        "# Only the reports are recorded; the locked snapshot and runtime state stay local.\n"
        "*\n!.gitignore\n!pilot_report.md\n!harvest.md\n"
    )
    shutil.copy2(verdict, run_dir / "verdict.md")
    if (draft / "adapters").is_dir():
        shutil.copytree(draft / "adapters", run_dir / "adapters")
        for p in (run_dir / "adapters").rglob("*"):
            if p.is_file():
                p.chmod(p.stat().st_mode | 0o111)

    m["run_id"] = run_id
    (run_dir / "swarm.json").write_text(json.dumps(m, indent=2))

    r = Run(run_dir)
    r.set("phase", "pilot")
    r.set("created_at", now())
    locks.write_locks(r)
    locks.advance_board_prefix(r)
    r.event("init", f"run {run_id} created; baseline {m['baseline_commit'][:12]}")
    print(run_dir)
    return 0


# ---------------------------------------------------------------------------------------------
# run loop


def _round_loop(r) -> str:
    max_rounds = int(r.budget()["max_rounds"])
    pilot_rounds = int((r.m.get("pilot") or {}).get("rounds", max_rounds))
    stopping = None
    for round_no in range(1, max_rounds + 1):
        bad = locks.check_control_locks(r)
        if bad:
            r.set("halt", "control files changed: " + ", ".join(bad))

        reason = scheduler.termination_reason(r, round_no)
        if reason:
            if reason.startswith("HALT:") and not r.get("halt"):
                r.set("halt", reason[len("HALT: "):])
            stopping = reason
            break

        scheduler.run_round(r, round_no)
        r.set("last_round", round_no)

        if round_no >= pilot_rounds and not r.get("pilot_report_written"):
            (r.dir / "pilot_report.md").write_text(reports.pilot_report(r))
            r.set("pilot_report_written", True)
            r.event("pilot_complete", f"pilot report written: {r.dir / 'pilot_report.md'}")
    else:
        stopping = budget.budget_reason(r, max_rounds) or f"budget: reached max_rounds {max_rounds}"
    return stopping


def cmd_run(args) -> int:
    r = Run(args.run_dir)
    pid_file = r.dir / "orchestrator.pid"
    existing = _read_pid(pid_file)
    if existing and _pid_alive(existing) and existing != os.getpid():
        raise CollabError(f"orchestrator already running (pid {existing})")

    halt = r.get("halt")
    if halt and not args.clear_halt:
        raise CollabError(
            f"swarm is halted: {halt}\nResolve it, then relaunch with --clear-halt (a human decision)."
        )
    if halt:
        r.event("halt_cleared", f"cleared by human: {halt}")
        r.set("halt", None)

    pid_file.write_text(str(os.getpid()))
    if r.get("run_started_at") is None:
        r.set("run_started_at", now())
    r.event("start", f"pid={os.getpid()}")

    stopping = None
    try:
        stopping = _round_loop(r)
    except Exception as exc:  # anything unexpected must halt loudly, never vanish
        r.set("halt", f"orchestrator crashed: {type(exc).__name__}: {exc}")
        r.event("crashed", traceback.format_exc())
        stopping = f"HALT: orchestrator crashed: {exc}"
    finally:
        pid_file.unlink(missing_ok=True)
        (r.dir / "STOP").unlink(missing_ok=True)

    r.event("stopped", stopping or "unknown")
    print(stopping or "stopped: unknown")
    return 1 if str(stopping).startswith("HALT:") else 0


def cmd_start(args) -> int:
    r = Run(args.run_dir)
    return _launch_detached(r, clear_halt=args.clear_halt)


def cmd_resume(args) -> int:
    r = Run(args.run_dir)
    halt = r.get("halt")
    if not halt:
        raise CollabError("not halted; use `start` to launch or continue a run")
    r.event("halt_cleared", f"cleared by human via resume: {halt}")
    r.set("halt", None)
    return _launch_detached(r, clear_halt=False)


def cmd_stop(args) -> int:
    r = Run(args.run_dir)
    pid = _running_pid(r)
    if not pid:
        print("not running")
        return 0
    if args.now:
        os.kill(pid, 15)  # SIGTERM
        print(f"sent SIGTERM to the orchestrator (pid {pid}); in-flight turns may be left running")
    else:
        (r.dir / "STOP").touch()
        print("STOP requested: the orchestrator will exit after the current round finishes")
    return 0


# ---------------------------------------------------------------------------------------------
# status / scale / board


def cmd_status(args) -> int:
    r = Run(args.run_dir)
    pid = _running_pid(r)
    t = budget.totals(r)
    b = r.budget()
    maximize = bool(r.m.get("maximize", True))
    agg = "MAX" if maximize else "MIN"
    best = r.db.execute(
        f"SELECT {agg}(full_score) AS best FROM results WHERE full_score IS NOT NULL"
    ).fetchone()["best"]

    print(f"run {r.run_id}  orchestrator={'RUNNING (pid ' + str(pid) + ')' if pid else 'not running'}"
          f"  halt={r.get('halt')}")
    print(f"round {r.get('last_round') or 0}/{b['max_rounds']}  cost ${t['cost']:.2f}/${b['max_usd']}"
          f"  tokens {t['tokens']}  turns {t['turns']}  running {t['running']}")
    print(f"best full_score: {best if best is not None else 'n/a'}  target: {r.m.get('target_score', 'n/a')}")
    print("\nrecent events:")
    for e in r.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 10").fetchall()[::-1]:
        print(f"  {e['at']} {e['kind']:<18} {e['agent'] or '':<16} {(e['detail'] or '')[:100]}")
    return 0


def cmd_scale(args) -> int:
    r = Run(args.run_dir)
    override = dict(r.get("budget_override", {}))
    if args.max_usd is not None:
        override["max_usd"] = args.max_usd
    if args.max_rounds is not None:
        override["max_rounds"] = args.max_rounds
    if args.max_hours is not None:
        override["max_hours"] = args.max_hours
    if not override:
        raise CollabError("scale requires at least one of --max-usd/--max-rounds/--max-hours")

    pilot_rounds = int((r.m.get("pilot") or {}).get("rounds", 0) or 0)
    new_max_rounds = override.get("max_rounds", r.budget()["max_rounds"])
    if pilot_rounds and new_max_rounds < pilot_rounds:
        raise CollabError(
            f"--max-rounds {new_max_rounds} is below pilot.rounds ({pilot_rounds}); the run would"
            " never reach the pilot round and pilot_report.md would never be written"
        )
    r.set("budget_override", override)
    r.event("scaled", f"budget_override={override}")
    print(f"budget override set: {override}\nContinue with: start {r.dir} --clear-halt (if halted) or start {r.dir}")
    return 0


def cmd_board(args) -> int:
    r = Run(args.run_dir)
    for e in board.read(r, limit=args.limit):
        body = e.get("body") or {}
        text = body.get("text") if isinstance(body, dict) else body
        if not isinstance(text, str):
            text = json.dumps(body, sort_keys=True)
        print(f"#{e['id']:<5} r{e['round']:<3} {e['type']:<10} {e['author_agent']:<18} {text[:120]}")
    return 0


# ---------------------------------------------------------------------------------------------
# harvest / cleanup / relock


def cmd_harvest(args) -> int:
    r = Run(args.run_dir)
    _refuse_if_running(r, "stop it before harvesting")
    text = reports.harvest(r)
    (r.dir / "harvest.md").write_text(text)
    print(text)
    return 0


def cmd_cleanup(args) -> int:
    """Remove this run's implementor worktrees/branches. Dry run by default.

    Every removal's actual exit code is checked: a failed `git worktree remove` or
    `git branch -D` (e.g. a dirty worktree, or a branch git refuses to force-drop) is
    logged as a `cleanup_warning` event and counted as failed, never silently treated
    as done just because it was attempted.
    """
    r = Run(args.run_dir)
    _refuse_if_running(r, "stop it first")

    removed_worktrees, failed_worktrees = [], []
    if r.lanes_root.exists():
        for child in sorted(p for p in r.lanes_root.iterdir() if p.is_dir()):
            print(f"{'remove' if args.confirm else 'would remove'} worktree {child}")
            if not args.confirm:
                continue
            proc = util.run(["git", "-C", str(r.repo), "worktree", "remove", "--force", str(child)],
                             check=False)
            if proc.returncode == 0:
                removed_worktrees.append(child)
            else:
                failed_worktrees.append(child)
                r.event("cleanup_warning", f"worktree not removed: {child}: {proc.stderr.strip()[-300:]}")

    branch_prefix = f"collab/{r.run_id}/"
    out = util.run(["git", "-C", str(r.repo), "branch", "--list", f"{branch_prefix}*"], check=False).stdout
    branches = [b.strip().lstrip("* ").strip() for b in out.splitlines() if b.strip()]
    removed_branches, failed_branches = [], []
    for branch in branches:
        print(f"{'delete' if args.confirm else 'would delete'} branch {branch}")
        if not args.confirm:
            continue
        proc = util.run(["git", "-C", str(r.repo), "branch", "-D", branch], check=False)
        if proc.returncode == 0:
            removed_branches.append(branch)
        else:
            failed_branches.append(branch)
            r.event("cleanup_warning", f"branch not deleted: {branch}: {proc.stderr.strip()[-300:]}")

    if not args.confirm:
        print("\ndry run; re-run with --confirm to apply")
        return 0

    if r.lanes_root.exists() and not any(r.lanes_root.iterdir()):
        r.lanes_root.rmdir()
    r.event(
        "cleanup",
        f"removed {len(removed_worktrees)}/{len(removed_worktrees) + len(failed_worktrees)} worktree(s), "
        f"{len(removed_branches)}/{len(removed_branches) + len(failed_branches)} branch(es)",
    )
    if failed_worktrees or failed_branches:
        print(f"\n{len(failed_worktrees)} worktree(s) and {len(failed_branches)} branch(es) could NOT be"
              " removed; see cleanup_warning events (status/board show them)")
        return 1
    return 0


def cmd_relock(args) -> int:
    r = Run(args.run_dir)
    _refuse_if_running(r, "stop it first")
    changed = locks.check_control_locks(r)
    data = json.loads((r.dir / "locks.json").read_text())
    data["control"] = {str(p): util.sha256_file(p) for p in locks.control_files(r)}
    (r.dir / "locks.json").write_text(json.dumps(data, indent=2))
    r.event("relocked", f"control files re-hashed by human; changed: {', '.join(changed) or 'none'}")
    print(f"relocked; changed files accepted: {', '.join(changed) or 'none'}")
    if r.get("halt"):
        print(f"halt is still set ({r.get('halt')}); clear it explicitly with: run/start --clear-halt")
    return 0


# ---------------------------------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="collab.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="validate a reviewed setup dir and create a run dir inside it")
    s.add_argument("setup_dir")
    s.set_defaults(fn=cmd_init)

    for name, fn in (("start", cmd_start), ("run", cmd_run)):
        s = sub.add_parser(name)
        s.add_argument("run_dir")
        s.add_argument("--clear-halt", action="store_true")
        s.set_defaults(fn=fn)

    s = sub.add_parser("status"); s.add_argument("run_dir"); s.set_defaults(fn=cmd_status)

    s = sub.add_parser("stop")
    s.add_argument("run_dir")
    s.add_argument("--now", action="store_true")
    s.set_defaults(fn=cmd_stop)

    s = sub.add_parser("resume"); s.add_argument("run_dir"); s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("scale")
    s.add_argument("run_dir")
    s.add_argument("--max-usd", type=float)
    s.add_argument("--max-rounds", type=int)
    s.add_argument("--max-hours", type=float)
    s.set_defaults(fn=cmd_scale)

    s = sub.add_parser("harvest"); s.add_argument("run_dir"); s.set_defaults(fn=cmd_harvest)

    s = sub.add_parser("cleanup")
    s.add_argument("run_dir")
    s.add_argument("--confirm", action="store_true")
    s.set_defaults(fn=cmd_cleanup)

    s = sub.add_parser("board")
    s.add_argument("run_dir")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_board)

    s = sub.add_parser("relock"); s.add_argument("run_dir"); s.set_defaults(fn=cmd_relock)

    args = p.parse_args(argv)
    try:
        return args.fn(args) or 0
    except CollabError as exc:
        print(f"collab.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
