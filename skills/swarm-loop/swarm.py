#!/usr/bin/env python3
"""swarm.py - orchestrator for /swarm-loop: parallel research lanes over one locked target.

Layout, inside the target repo so the setup and reports can be reviewed and committed:
  <repo>/docs/swarm/<name>/            reviewed setup: verdict.md, swarm.json, themes.md, probe.md,
                                       adapters/
  <repo>/docs/swarm/<name>/<run-id>/   one run: pilot_report.md and harvest.md are committed; the
                                       locked copy of the setup and runtime state are gitignored

Commands:
  init <setup-dir>                  validate a reviewed setup dir and create a run dir inside it,
                                    with the ledger and lock manifest
  start <run-dir> [--clear-halt]    launch the orchestrator detached (caffeinate + new session);
                                    a halted swarm needs --clear-halt, which is a human decision
  run <run-dir> [--clear-halt]      the orchestrator loop itself (what `start` launches)
  status <run-dir>                  phase, lanes, totals, budget, global best, recent events
  stop <run-dir> [--now]            finish running iterations then exit; --now kills them
  scale <run-dir> --lanes N --model M [--max-usd X] [--max-iterations N] [--max-hours H]
                                    approve the full run after the pilot (pilot lanes keep going),
                                    or change lanes/budget of a full run between starts
  registry <run-dir> [--limit N]    hypotheses tried so far, across lanes (lanes call this)
  harvest <run-dir>                 stability re-check + deflated Sharpe, pick <= 3 finalists,
                                    print their lockbox commands (never runs them)
  record-lockbox <run-dir> <finalist> --score X --passed yes|no [--note TEXT]
  relock <run-dir>                  accept intentional changes to control files (swarm.py, adapters);
                                    a human decision, logged
  cleanup <run-dir> [--confirm]     tag + remove non-finalist lane worktrees (dry run by default)

Standard library only; Python 3.9+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import time
import traceback
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
PREAMBLE = SKILL_DIR / "lane_preamble.md"
POLL_SECONDS = float(os.environ.get("SWARM_POLL_SECONDS", "5"))
VERDICTS = {"ACCEPTED", "ROLLBACK", "REJECTED", "NONE"}
MODE_TEXT = {
    "target_record": (
        "The target's own loop decides ACCEPTED or ROLLBACK and commits accordingly. Follow its\n"
        "instructions below exactly; the swarm reads the verdict from the target's records afterwards."
    ),
    "swarm_eval": (
        "Make ONE focused change for your hypothesis, run the target's fast tests, and finish with the\n"
        "change left uncommitted in this worktree (new files included). Leave committing to the swarm:\n"
        "after you exit it commits everything as one trial on this branch, evaluates it itself, keeps it\n"
        "if the score improves, and resets the branch if not. Every evaluated trial is tagged, so nothing\n"
        "you try is lost. Your own measurements guide you; they are not recorded."
    ),
}


class SwarmError(Exception):
    """A condition the human must see; never swallowed."""


# ---------------------------------------------------------------------------------------------
# small helpers


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def run(cmd, cwd=None, env=None, check=True, timeout=None) -> subprocess.CompletedProcess:
    shell = isinstance(cmd, str)
    proc = subprocess.run(
        f"set -o pipefail; {cmd}" if shell else cmd, cwd=cwd, env=env, capture_output=True, text=True,
        timeout=timeout, shell=shell, executable="/bin/bash" if shell else None,
    )
    if check and proc.returncode != 0:
        shown = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
        raise SwarmError(f"command failed ({proc.returncode}): {shown}\n{proc.stderr.strip()}")
    return proc


def git(repo, *args, check=True) -> str:
    return run(["git", "-C", str(repo), *args], check=check).stdout.strip()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def render(template: str, values: dict) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def kill_lane(proc, sig) -> None:
    """Signal a lane's process group; fall back to the process itself if the group is gone."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        if proc.poll() is None:
            proc.send_signal(sig)


def notify(message: str) -> None:
    if shutil.which("osascript"):
        subprocess.run(
            ["osascript", "-e", f'display notification {json.dumps(message)} with title "swarm-loop"'],
            capture_output=True,
        )


# ---------------------------------------------------------------------------------------------
# manifest


REQUIRED_KEYS = [
    "name", "mode", "repo", "workdir", "baseline_commit", "request", "iteration_prompt", "locked_files",
    "lockbox_command", "trials", "themes", "lanes", "models", "budget",
]
MODES = ("target_record", "swarm_eval")


def prompt_in_run_dir(m: dict) -> bool:
    return str(m.get("iteration_prompt", "")).startswith("adapters/")


def validate_manifest(m: dict, draft: Path) -> list:
    errors = []
    for key in REQUIRED_KEYS:
        if key not in m:
            errors.append(f"missing key: {key}")
    if errors:
        return errors
    repo = Path(m["repo"])
    if not (repo / ".git").exists():
        errors.append(f"repo is not a git checkout: {repo}")
        return errors
    base = m["baseline_commit"]
    if run(["git", "-C", str(repo), "cat-file", "-e", f"{base}^{{commit}}"], check=False).returncode:
        errors.append(f"baseline_commit does not resolve: {base}")
        return errors
    if m["mode"] not in MODES:
        errors.append(f"mode must be one of {MODES}")
    in_run_dir = prompt_in_run_dir(m)
    repo_paths = list(m["locked_files"]) + ([] if in_run_dir else [m["iteration_prompt"]])
    for rel in repo_paths:
        path = str(Path(m["workdir"]) / rel) if m["workdir"] not in ("", ".") else rel
        if run(["git", "-C", str(repo), "cat-file", "-e", f"{base}:{path}"], check=False).returncode:
            errors.append(f"not tracked at baseline: {path}")
    if in_run_dir:
        if not (draft / m["iteration_prompt"]).is_file():
            errors.append(f"iteration prompt missing: {m['iteration_prompt']}")
    elif m["iteration_prompt"] not in m["locked_files"]:
        errors.append("a repo iteration_prompt must be listed in locked_files")
    needed = {"target_record": ["result_command", "verify_command"], "swarm_eval": ["eval_command"]}.get(m["mode"], [])
    for key in ("lockbox_command", *needed):
        if not isinstance(m.get(key), str) or not m[key].strip():
            errors.append(f"{key} must be a non-empty command string (mode {m['mode']})")
    if m["mode"] == "swarm_eval" and not isinstance(m.get("min_improvement", 0), (int, float)):
        errors.append("min_improvement must be a number")
    tol = m.get("verify_tolerance", 0.02)
    if not (isinstance(tol, (int, float)) and 0 <= tol < 1):
        errors.append("verify_tolerance must be a fraction in [0, 1)")
    trials = m["trials"]
    if not isinstance(trials.get("per_iteration_estimate"), int) or trials["per_iteration_estimate"] < 1:
        errors.append("trials.per_iteration_estimate must be a positive integer (conservative fallback)")
    if not isinstance(m.get("maximize", True), bool):
        errors.append("maximize must be true or false")
    themes = m["themes"]
    if not themes:
        errors.append("themes must not be empty")
    ids = [t.get("id") for t in themes]
    if len(set(ids)) != len(ids) or None in ids:
        errors.append("every theme needs a unique id")
    for t in themes:
        for key in ("title", "scope", "freeze_dirs"):
            if key not in t:
                errors.append(f"theme {t.get('id')}: missing {key}")
    lanes = m["lanes"]
    if not (isinstance(lanes.get("pilot"), int) and lanes["pilot"] >= 1):
        errors.append("lanes.pilot must be >= 1")
    if not (isinstance(lanes.get("max"), int) and lanes["max"] >= lanes.get("pilot", 1)):
        errors.append("lanes.max must be >= lanes.pilot")
    models = m["models"]
    if not models.get("pilot_mix"):
        errors.append("models.pilot_mix must list at least one model")
    budget = m["budget"]
    if not (isinstance(budget.get("max_usd"), (int, float)) and budget["max_usd"] > 0):
        errors.append("budget.max_usd is required and must be > 0")
    for key in ("max_iterations", "max_hours", "stall_k"):
        if not (isinstance(budget.get(key), (int, float)) and budget[key] > 0):
            errors.append(f"budget.{key} must be > 0")
    pilot = m.get("pilot", {})
    if not (isinstance(pilot.get("iterations"), int) and pilot["iterations"] >= 1):
        errors.append("pilot.iterations must be >= 1")
    for adapter in m.get("adapters", []):
        if not (draft / "adapters" / adapter).is_file():
            errors.append(f"adapter file missing: adapters/{adapter}")
    return errors


# ---------------------------------------------------------------------------------------------
# run state


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lanes (
  id TEXT PRIMARY KEY, seq INTEGER NOT NULL, theme_id TEXT NOT NULL, model TEXT NOT NULL,
  state TEXT NOT NULL, branch TEXT NOT NULL, worktree TEXT NOT NULL, start_commit TEXT NOT NULL,
  parent_lane TEXT, env_json TEXT NOT NULL, created_at TEXT NOT NULL, ended_at TEXT, end_reason TEXT
);
CREATE TABLE IF NOT EXISTS iterations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lane TEXT NOT NULL, n INTEGER NOT NULL, model TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, exit_code INTEGER, is_error INTEGER, timed_out INTEGER,
  cost_usd REAL, turns INTEGER, verdict TEXT, score REAL, trials INTEGER, trials_exact INTEGER,
  ref TEXT, hypothesis TEXT, commit_before TEXT NOT NULL, commit_after TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, lane TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS lockbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, finalist INTEGER NOT NULL, iteration_id INTEGER NOT NULL,
  commit_sha TEXT NOT NULL, score REAL NOT NULL, passed INTEGER NOT NULL, at TEXT NOT NULL, note TEXT
);
"""


class Run:
    def __init__(self, run_dir: Path):
        self.dir = Path(run_dir).expanduser().resolve()
        manifest = self.dir / "swarm.json"
        if not manifest.is_file():
            raise SwarmError(f"not a swarm run dir (no swarm.json): {self.dir}")
        self.m = json.loads(manifest.read_text())
        self.db = sqlite3.connect(str(self.dir / "ledger.sqlite"), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.repo = Path(self.m["repo"])
        self.run_id = self.m["run_id"]
        self.lanes_root = self.repo.parent / f"{self.repo.name}-swarm-{self.run_id}"
        self.maximize = self.m.get("maximize", True)

    # meta
    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, json.dumps(value)))
        self.db.commit()

    def event(self, kind, detail, lane=None):
        self.db.execute("INSERT INTO events(at, kind, lane, detail) VALUES(?,?,?,?)", (now(), kind, lane, detail))
        self.db.commit()
        self.log(f"[{kind}]{' ' + lane if lane else ''} {detail}")

    def log(self, message):
        line = f"[{now()}] {message}"
        with open(self.dir / "orchestrator.log", "a") as fh:
            fh.write(line + "\n")
        print(line, file=sys.stderr, flush=True)

    # budget (scale may override the manifest's)
    def budget(self):
        b = dict(self.m["budget"])
        b.update(self.get("budget_override", {}))
        return b

    def workdir(self, worktree: Path) -> Path:
        wd = self.m["workdir"]
        return worktree if wd in ("", ".") else worktree / wd

    # ledger queries
    def totals(self):
        row = self.db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost, COALESCE(SUM(trials),0) AS trials,"
            " SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END) AS running FROM iterations"
        ).fetchone()
        return dict(row)

    def global_best(self):
        order = "DESC" if self.maximize else "ASC"
        return self.db.execute(
            "SELECT * FROM iterations WHERE verdict='ACCEPTED' AND score IS NOT NULL"
            f" AND commit_after IS NOT NULL AND commit_after != commit_before ORDER BY score {order}, id LIMIT 1"
        ).fetchone()


# ---------------------------------------------------------------------------------------------
# locks


def control_files(r: Run) -> list:
    files = [r.dir / "swarm.json", r.dir / "verdict.md", PREAMBLE, Path(__file__).resolve()]
    adapters = r.dir / "adapters"
    if adapters.is_dir():
        files += sorted(p for p in adapters.rglob("*")
                        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    return files


def write_locks(r: Run):
    base = r.m["baseline_commit"]
    target = {}
    for rel in r.m["locked_files"]:
        path = rel if r.m["workdir"] in ("", ".") else f"{r.m['workdir']}/{rel}"
        blob = subprocess.run(["git", "-C", str(r.repo), "show", f"{base}:{path}"], capture_output=True, check=True).stdout
        target[rel] = sha256_bytes(blob)
    control = {str(p): sha256_file(p) for p in control_files(r)}
    (r.dir / "locks.json").write_text(json.dumps({"target": target, "control": control}, indent=2))


def check_control_locks(r: Run) -> list:
    locks = json.loads((r.dir / "locks.json").read_text())
    bad = []
    for path, digest in locks["control"].items():
        p = Path(path)
        if not p.is_file() or sha256_file(p) != digest:
            bad.append(path)
    return bad


def check_lane_locks(r: Run, worktree: Path) -> list:
    locks = json.loads((r.dir / "locks.json").read_text())
    wd = r.workdir(worktree)
    bad = []
    for rel, digest in locks["target"].items():
        p = wd / rel
        if not p.is_file() or sha256_file(p) != digest:
            bad.append(rel)
    return bad


# ---------------------------------------------------------------------------------------------
# init


def cmd_init(args):
    draft = Path(args.draft).expanduser().resolve()
    verdict = draft / "verdict.md"
    if not verdict.is_file():
        raise SwarmError(f"{verdict} missing: run the fitness review first")
    if not re.search(r"^VERDICT:\s*GO\b", verdict.read_text(), re.MULTILINE):
        raise SwarmError("verdict.md does not contain 'VERDICT: GO'; the swarm only launches on GO")
    m = json.loads((draft / "swarm.json").read_text())
    errors = validate_manifest(m, draft)
    if errors:
        raise SwarmError("manifest invalid:\n  " + "\n  ".join(errors))
    repo = Path(m["repo"])
    m["run_id"] = f"{re.sub(r'[^A-Za-z0-9_-]', '-', m['name'])}-{dt.datetime.now():%Y%m%d-%H%M}"
    run_dir = draft / m["run_id"]
    if run_dir.exists():
        raise SwarmError(f"run dir already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    (run_dir / "logs").mkdir()
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
    for extra in ("themes.md", "probe.md", "README.md"):
        if (draft / extra).is_file():
            shutil.copy2(draft / extra, run_dir / extra)
    (run_dir / "swarm.json").write_text(json.dumps(m, indent=2))
    r = Run(run_dir)
    r.set("phase", "pilot")
    r.set("target_lanes", m["lanes"]["pilot"])
    r.set("created_at", now())
    r.set("theme_cursor", 0)
    write_locks(r)
    r.event("init", f"run {m['run_id']} created; baseline {m['baseline_commit'][:12]}")
    print(run_dir)


# ---------------------------------------------------------------------------------------------
# lanes


def next_lane_id(r: Run) -> tuple:
    seq = r.db.execute("SELECT COALESCE(MAX(seq),0)+1 AS s FROM lanes").fetchone()["s"]
    return f"L{seq:02d}", seq


def pick_theme(r: Run, fallback_theme: str | None) -> str:
    themes = [t["id"] for t in r.m["themes"]]
    used = {row["theme_id"] for row in r.db.execute("SELECT theme_id FROM lanes")}
    for t in themes:
        if t not in used:
            return t
    if fallback_theme:
        return fallback_theme
    cursor = r.get("theme_cursor", 0)
    r.set("theme_cursor", cursor + 1)
    return themes[cursor % len(themes)]


def pick_model(r: Run, seq: int) -> str:
    if r.get("phase") == "pilot":
        mix = r.m["models"]["pilot_mix"]
        return mix[(seq - 1) % len(mix)]
    return r.get("full_model") or r.m["models"].get("default") or r.m["models"]["pilot_mix"][0]


def lane_base_env(r: Run) -> dict:
    env = dict(os.environ)
    for key in r.m.get("strip_env", []):
        env.pop(key, None)
    for key in list(env):
        if key.startswith("SWARM_"):
            env.pop(key)
    env.update({k: str(v) for k, v in r.m.get("env", {}).items()})
    env["SWARM_RUN_DIR"] = str(r.dir)
    env["SWARM_RUN_ID"] = r.run_id
    return env


def create_lane(r: Run, theme_id: str, start_commit: str, parent: str | None):
    lane_id, seq = next_lane_id(r)
    model = pick_model(r, seq)
    branch = f"swarm/{r.run_id}/{lane_id}"
    worktree = r.lanes_root / lane_id
    r.lanes_root.mkdir(parents=True, exist_ok=True)
    git(r.repo, "worktree", "add", "-q", "-b", branch, str(worktree), start_commit)
    wd = r.workdir(worktree)

    # Protect swarm files from the target's own cleanup (e.g. `git clean -fd` honours excludes).
    exclude = Path(git(r.repo, "rev-parse", "--git-common-dir"))
    if not exclude.is_absolute():
        exclude = r.repo / exclude
    exclude = exclude / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    wanted = [".swarm/"]
    src_claude = r.repo / ".claude"
    claude_tracked = bool(git(r.repo, "ls-files", ".claude"))
    if src_claude.is_dir() and not claude_tracked:
        shutil.copytree(src_claude, wd / ".claude", dirs_exist_ok=True)
        rel = wd.relative_to(worktree)
        wanted.append("/.claude/" if str(rel) == "." else f"/{rel}/.claude/")
    existing = exclude.read_text().splitlines() if exclude.exists() else []
    with open(exclude, "a") as fh:
        for pattern in wanted:
            if pattern not in existing:
                fh.write(pattern + "\n")
    (wd / ".swarm").mkdir(exist_ok=True)

    env = lane_base_env(r)
    env.update({"SWARM_LANE": lane_id, "SWARM_LANE_DIR": str(worktree), "SWARM_WORKDIR": str(wd)})
    lane_env = {}
    for cmd in r.m.get("lane_setup", []):
        proc = run(render(cmd, {"run_dir": r.dir, "lane": lane_id, "lane_dir": worktree, "workdir": wd}),
                   cwd=wd, env={**env, **lane_env}, check=False, timeout=900)
        if proc.returncode != 0:
            r.db.execute(
                "INSERT INTO lanes(id, seq, theme_id, model, state, branch, worktree, start_commit, parent_lane,"
                " env_json, created_at, ended_at, end_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lane_id, seq, theme_id, model, "setup_failed", branch, str(worktree), start_commit, parent,
                 "{}", now(), now(), proc.stderr.strip()[-500:]),
            )
            r.db.commit()
            r.event("setup_failed", f"lane setup command failed: {cmd}\n{proc.stderr.strip()[-500:]}", lane_id)
            return None
        for line in proc.stdout.splitlines():
            match = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
            if match:
                lane_env[match.group(1)] = match.group(2)
    if r.m["mode"] == "swarm_eval":
        try:
            res = run_eval(r, worktree, start_commit, lane_id, lane_env)
            r.set(f"lane_baseline:{lane_id}", float(res["score"]))
        except (SwarmError, KeyError, TypeError, ValueError) as exc:
            r.db.execute(
                "INSERT INTO lanes(id, seq, theme_id, model, state, branch, worktree, start_commit, parent_lane,"
                " env_json, created_at, ended_at, end_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lane_id, seq, theme_id, model, "setup_failed", branch, str(worktree), start_commit, parent,
                 json.dumps(lane_env), now(), now(), f"baseline eval failed: {exc}"[:500]),
            )
            r.db.commit()
            r.event("setup_failed", f"baseline eval failed: {exc}", lane_id)
            return None
    r.db.execute(
        "INSERT INTO lanes(id, seq, theme_id, model, state, branch, worktree, start_commit, parent_lane, env_json,"
        " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (lane_id, seq, theme_id, model, "active", branch, str(worktree), start_commit, parent,
         json.dumps(lane_env), now()),
    )
    r.db.commit()
    r.event("lane_created", f"theme={theme_id} model={model} from={start_commit[:12]} parent={parent}", lane_id)
    return lane_id


def retire_lane(r: Run, lane_id: str, state: str, reason: str):
    r.db.execute("UPDATE lanes SET state=?, ended_at=?, end_reason=? WHERE id=?", (state, now(), reason, lane_id))
    r.db.commit()
    r.event("lane_" + state, reason, lane_id)


# ---------------------------------------------------------------------------------------------
# iterations


def theme(r: Run, theme_id: str) -> dict:
    return next(t for t in r.m["themes"] if t["id"] == theme_id)


def render_prompt(r: Run, lane, n: int) -> str:
    t = theme(r, lane["theme_id"])
    wd = r.workdir(Path(lane["worktree"]))
    values = {
        "lane": lane["id"], "run_id": r.run_id, "n": n, "request": r.m["request"],
        "theme_title": t["title"], "theme_scope": t["scope"],
        "freeze_dirs": ", ".join(t["freeze_dirs"]) or "(whole target)",
        "registry_cmd": f"python3 {Path(__file__).resolve()} registry {r.dir}",
        "iteration_prompt": r.m["iteration_prompt"],
        "mode_instructions": render(MODE_TEXT[r.m["mode"]], {"lane": lane["id"], "n": n}),
    }
    preamble = render(PREAMBLE.read_text(), values)
    source = r.dir / r.m["iteration_prompt"] if prompt_in_run_dir(r.m) else wd / r.m["iteration_prompt"]
    target_prompt = source.read_text()
    return preamble + "\n\n---\n\n" + target_prompt


def launch_iteration(r: Run, lane, running: dict):
    worktree = Path(lane["worktree"])
    if not worktree.is_dir():
        retire_lane(r, lane["id"], "errored", f"worktree missing: {worktree}")
        return
    wd = r.workdir(worktree)
    n = r.db.execute("SELECT COUNT(*) AS c FROM iterations WHERE lane=?", (lane["id"],)).fetchone()["c"] + 1
    logs = r.dir / "logs" / lane["id"]
    logs.mkdir(parents=True, exist_ok=True)
    stem = logs / f"iter-{n:03d}"
    prompt_path = stem.with_suffix(".prompt.md")
    prompt_path.write_text(render_prompt(r, lane, n))
    hyp = wd / ".swarm" / "hypothesis.txt"
    if hyp.exists():
        hyp.unlink()

    t = theme(r, lane["theme_id"])
    env = lane_base_env(r)
    env.update(json.loads(lane["env_json"]))
    env.update({"SWARM_LANE": lane["id"], "SWARM_LANE_DIR": str(worktree), "SWARM_WORKDIR": str(wd),
                "SWARM_ITERATION": str(n)})
    if t["freeze_dirs"]:
        env["CLAUDE_FREEZE_DIR"] = ":".join(t["freeze_dirs"])
    cmd = ["claude", "-p", "--output-format", "json",
           "--permission-mode", r.m.get("permission_mode", "auto"), "--model", lane["model"]]
    if r.m["budget"].get("iteration_usd_cap"):
        cmd += ["--max-budget-usd", str(r.m["budget"]["iteration_usd_cap"])]
    cmd += ["--disallowedTools", "Bash(git push:*)", "Bash(gh:*)", *r.m.get("disallowed_tools", [])]

    before = git(worktree, "rev-parse", "HEAD")
    cur = r.db.execute(
        "INSERT INTO iterations(lane, n, model, started_at, commit_before) VALUES(?,?,?,?,?)",
        (lane["id"], n, lane["model"], now(), before),
    )
    r.db.commit()
    with open(prompt_path) as stdin, open(stem.with_suffix(".json"), "w") as out, \
            open(stem.with_suffix(".err"), "w") as err:
        proc = subprocess.Popen(cmd, cwd=wd, env=env, stdin=stdin, stdout=out, stderr=err, start_new_session=True)
    running[lane["id"]] = {"proc": proc, "iter_id": cur.lastrowid, "n": n, "stem": stem, "started": time.time()}
    r.log(f"{lane['id']} iteration {n} started (model {lane['model']}, theme {lane['theme_id']})")


def read_result(r: Run, lane, n: int) -> dict:
    """Run the target's result adapter. The swarm never takes a verdict or score from the lane agent."""
    worktree = Path(lane["worktree"])
    wd = r.workdir(worktree)
    last_ref = r.db.execute(
        "SELECT ref FROM iterations WHERE lane=? AND ref IS NOT NULL ORDER BY id DESC LIMIT 1", (lane["id"],)
    ).fetchone()
    env = lane_base_env(r)
    env.update(json.loads(lane["env_json"]))
    env.update({"SWARM_LANE": lane["id"], "SWARM_LANE_DIR": str(worktree), "SWARM_WORKDIR": str(wd),
                "SWARM_ITERATION": str(n), "SWARM_LAST_REF": last_ref["ref"] if last_ref else ""})
    cmd = render(r.m["result_command"], {"run_dir": r.dir, "lane": lane["id"], "lane_dir": worktree, "workdir": wd})
    proc = run(cmd, cwd=wd, env=env, check=False, timeout=600)
    if proc.returncode != 0:
        raise SwarmError(f"result adapter exited {proc.returncode}: {proc.stderr.strip()[-400:]}")
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise SwarmError(f"result adapter printed no JSON on its last line: {proc.stdout[-400:]!r}") from exc
    verdict = str(result.get("verdict", "")).upper()
    if verdict not in VERDICTS:
        raise SwarmError(f"result adapter verdict {verdict!r} not in {sorted(VERDICTS)}")
    if verdict == "ACCEPTED" and not isinstance(result.get("score"), (int, float)):
        raise SwarmError("result adapter reported ACCEPTED without a numeric score")
    result["verdict"] = verdict
    return result


def run_eval(r: Run, worktree: Path, commit: str, lane_id: str, lane_env: dict, extra=None,
             parent: str = "") -> dict:
    """Run the locked eval command on the commit checked out in `worktree`. Swarm-run, never agent-run.

    `parent` is the commit the candidate builds on (SWARM_PARENT). Empty means there is no candidate:
    the eval scores the commit as a lane's reference point."""
    wd = r.workdir(worktree)
    extra = extra or {}
    env = lane_base_env(r)
    env.update(lane_env)
    env.update({"SWARM_LANE": lane_id, "SWARM_LANE_DIR": str(worktree), "SWARM_WORKDIR": str(wd),
                "SWARM_COMMIT": commit, "SWARM_PARENT": parent,
                **{f"SWARM_{k.upper()}": str(v) for k, v in extra.items()}})
    cmd = render(r.m["eval_command"], {"run_dir": r.dir, "lane": lane_id, "lane_dir": worktree, "workdir": wd,
                                       "commit": commit, **extra})
    proc = run(cmd, cwd=wd, env=env, check=False, timeout=float(r.m.get("eval_timeout_min", 120)) * 60)
    if proc.returncode != 0:
        raise SwarmError(f"eval exited {proc.returncode}: {proc.stderr.strip()[-400:]}")
    try:
        res = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise SwarmError(f"eval printed no JSON on its last line: {proc.stdout[-300:]!r}") from exc
    if not isinstance(res.get("score"), (int, float)) or isinstance(res.get("score"), bool):
        raise SwarmError(f"eval JSON has no numeric score: {res}")
    return res


def swarm_eval_result(r: Run, lane, before: str, after: str, n: int) -> dict:
    """swarm_eval mode: tag the candidate, evaluate it, keep it or reset the branch."""
    worktree = Path(lane["worktree"])
    lane_id = lane["id"]
    if git(worktree, "status", "--porcelain"):
        # The lane leaves its change uncommitted; the swarm commits it as the trial. The repo's own
        # commit hooks are for changes headed to a real branch and are skipped on swarm branches;
        # the eval's gates apply instead, and the hooks run again when a finalist is merged.
        hyp = r.workdir(worktree) / ".swarm" / "hypothesis.txt"
        subject = (hyp.read_text().strip().splitlines() or ["(no hypothesis)"])[0][:200] if hyp.exists() \
            else "(no hypothesis)"
        git(worktree, "add", "-A")
        git(worktree, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--no-verify",
            "-m", f"swarm {lane_id} it{n}: {subject}")
        after = git(worktree, "rev-parse", "HEAD")
    if after == before:
        return {"verdict": "NONE", "note": "no change", "commit": after}
    count = int(git(worktree, "rev-list", "--count", f"{before}..{after}"))
    committed = after
    tag = f"swarm-trials/{r.run_id}/{lane_id}/{n:03d}"
    git(r.repo, "tag", "-f", tag, after)
    note = f"trial tagged {tag}" + (f"; {count} commits squashed into one trial" if count > 1 else "")
    try:
        res = run_eval(r, worktree, after, lane_id, json.loads(lane["env_json"]), parent=before)
    except SwarmError as exc:
        git(worktree, "reset", "-q", "--hard", before)
        return {"verdict": "REJECTED", "note": f"{note}; eval failed, branch reset: {exc}", "ref": tag,
                "commit": committed}
    order = "MAX" if r.maximize else "MIN"
    lane_best = r.db.execute(
        f"SELECT {order}(score) AS b FROM iterations WHERE lane=? AND verdict='ACCEPTED'", (lane_id,)
    ).fetchone()["b"]
    reference = lane_best if lane_best is not None else r.get(f"lane_baseline:{lane_id}")
    margin = float(r.m.get("min_improvement", 0))
    score = float(res["score"])
    better = score > reference + margin if r.maximize else score < reference - margin
    detail = res.get("detail") or {}
    out = {"score": score, "ref": tag, "trials": res.get("trials"), "hypothesis": res.get("hypothesis"),
           "commit": committed, "note": f"{note}; reference {reference}"
           + (f"; rejected: {detail['rejected']}" if detail.get("rejected") else "")}
    if better:
        out["verdict"] = "ACCEPTED"
    else:
        git(worktree, "reset", "-q", "--hard", before)
        out["verdict"] = "ROLLBACK"
        out["note"] += "; branch reset"
    return out


def verify_accepted(r: Run, lane, commit: str, recorded: float) -> tuple:
    """target_record: re-score an ACCEPTED commit with the locked verify_command in a clean worktree.

    The target's recorded score is a claim made during an iteration the lane agent controlled (it could
    edit the scorer, record, and revert before the lock check). The ledger keeps the swarm's own score.
    Returns (verified_score, disputed_reason_or_None). Raises SwarmError if verification cannot run."""
    wt = temp_worktree(r, commit, f"verify-{lane['id']}")
    try:
        tampered = check_lane_locks(r, wt)
        if tampered:
            return None, "committed tree changes locked files: " + ", ".join(tampered)
        res = adapter_json(r, r.m["verify_command"], wt, commit, {}, json.loads(lane["env_json"]))
        if not isinstance(res.get("score"), (int, float)) or isinstance(res.get("score"), bool):
            raise SwarmError(f"verify_command JSON has no numeric score: {res}")
        verified = float(res["score"])
    finally:
        git(r.repo, "worktree", "remove", "--force", str(wt), check=False)
        if wt.exists():
            r.event("cleanup_warning", f"could not remove verify worktree {wt}", lane["id"])
    tol = float(r.m.get("verify_tolerance", 0.02))
    slack = abs(recorded) * tol + 1e-12
    worse = verified < recorded - slack if r.maximize else verified > recorded + slack
    if worse:
        return verified, (f"recorded score {recorded} but swarm verification scored {verified}"
                          f" (tolerance {tol:.0%}); possible forged record")
    return verified, None


def finalize_iteration(r: Run, lane_id: str, info: dict, timed_out: bool):
    lane = r.db.execute("SELECT * FROM lanes WHERE id=?", (lane_id,)).fetchone()
    worktree = Path(lane["worktree"])
    wd = r.workdir(worktree)
    rc = info["proc"].returncode
    cost, turns, is_error = 0.0, None, True
    try:
        payload = json.loads(Path(info["stem"]).with_suffix(".json").read_text())
        cost = float(payload.get("total_cost_usd") or 0.0)
        turns = payload.get("num_turns")
        is_error = bool(payload.get("is_error", False))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        r.event("output_unparsed", f"iteration {info['n']}: {exc}", lane_id)
    failed = timed_out or rc != 0 or is_error
    after = git(worktree, "rev-parse", "HEAD")
    before = r.db.execute("SELECT commit_before FROM iterations WHERE id=?", (info["iter_id"],)).fetchone()[0]

    tampered = check_lane_locks(r, worktree)
    verdict, score, trials, exact, ref, hypothesis, notes = "NONE", None, None, 0, None, None, []
    if tampered:
        notes.append("locked files changed: " + ", ".join(tampered))
    elif r.m["mode"] == "swarm_eval":
        try:
            res = swarm_eval_result(r, lane, before, after, info["n"])
            after = res.get("commit", after)
            verdict, score, ref = res["verdict"], res.get("score"), res.get("ref")
            hypothesis = res.get("hypothesis")
            if isinstance(res.get("trials"), int) and res["trials"] >= 0:
                trials, exact = res["trials"], 1
            if res.get("note"):
                notes.append(res["note"])
            after_eval = git(worktree, "rev-parse", "HEAD")
            if verdict == "ACCEPTED":
                pass
            elif after_eval != before:
                raise SwarmError(f"branch not reset after {verdict}: HEAD {after_eval} != {before}")
        except SwarmError as exc:
            verdict, score, ref, hypothesis, trials, exact = "NONE", None, None, None, None, 0
            notes.append(f"swarm eval error: {exc}")
            reset = run(["git", "-C", str(worktree), "reset", "-q", "--hard", before], check=False)
            if reset.returncode != 0:
                r.set("halt", f"lane {lane_id}: could not reset to {before} after eval error: {reset.stderr.strip()}")
            r.set(f"adapter_errors:{lane_id}", r.get(f"adapter_errors:{lane_id}", 0) + 1)
            r.event("adapter_error", str(exc), lane_id)
        else:
            r.set(f"adapter_errors:{lane_id}", 0)
    else:
        try:
            res = read_result(r, lane, info["n"])
            seen = r.db.execute("SELECT 1 FROM iterations WHERE lane=? AND ref=?", (lane_id, str(res.get("ref")))).fetchone()
            if res.get("ref") is not None and seen:
                notes.append(f"result ref {res.get('ref')} already recorded; no new result")
            else:
                verdict, score, ref = res["verdict"], res.get("score"), res.get("ref")
                hypothesis = res.get("hypothesis")
                if isinstance(res.get("trials"), int) and res["trials"] >= 0:
                    trials, exact = res["trials"], 1
            r.set(f"adapter_errors:{lane_id}", 0)
        except SwarmError as exc:
            notes.append(f"result adapter error: {exc}")
            count = r.get(f"adapter_errors:{lane_id}", 0) + 1
            r.set(f"adapter_errors:{lane_id}", count)
            r.event("adapter_error", str(exc), lane_id)
    # ACCEPTED_UNCOMMITTED is deliberately outside VERDICTS: never eligible as a best or finalist.
    if verdict == "ACCEPTED" and after == before:
        notes.append("ACCEPTED but no new commit; not eligible as a best")
        verdict = "ACCEPTED_UNCOMMITTED"
    disputed = None
    if verdict == "ACCEPTED" and r.m["mode"] == "target_record":
        try:
            verified, disputed = verify_accepted(r, lane, after, float(score))
        except SwarmError as exc:
            verified, disputed = None, f"verification could not run: {exc}"
        if disputed:
            notes.append(f"DISPUTED: {disputed}")
            verdict, score = "DISPUTED", verified
        else:
            notes.append(f"verified by swarm: recorded {score}, verified {verified}")
            score = verified
    if trials is None:
        trials = int(r.m["trials"]["per_iteration_estimate"])  # conservative: every iteration pays it
    if not hypothesis:
        hyp = wd / ".swarm" / "hypothesis.txt"
        hypothesis = hyp.read_text().strip()[:500] if hyp.exists() else None
    if timed_out:
        notes.append("timed out")
    r.db.execute(
        "UPDATE iterations SET ended_at=?, exit_code=?, is_error=?, timed_out=?, cost_usd=?, turns=?, verdict=?,"
        " score=?, trials=?, trials_exact=?, ref=?, hypothesis=?, commit_after=?, note=? WHERE id=?",
        (now(), rc, int(is_error), int(timed_out), cost, turns, verdict, score, trials, exact,
         None if ref is None else str(ref), hypothesis, after, "; ".join(notes) or None, info["iter_id"]),
    )
    r.db.commit()
    r.log(f"{lane_id} iteration {info['n']} -> {verdict} score={score} cost=${cost:.2f} rc={rc}"
          f"{' error' if failed else ''}{' | ' + '; '.join(notes) if notes else ''}")

    if tampered:
        retire_lane(r, lane_id, "quarantined", "locked files changed: " + ", ".join(tampered))
        r.set("halt", f"lane {lane_id} changed locked files ({', '.join(tampered)}); inspect {worktree}")
        return
    if disputed:
        retire_lane(r, lane_id, "quarantined", disputed)
        r.set("halt", f"lane {lane_id} iteration {info['n']}: {disputed}; inspect {worktree} and the target's records")
        return
    if r.get(f"adapter_errors:{lane_id}", 0) >= 3:
        r.set("halt", f"result adapter failed 3 times in a row on {lane_id}; fix the adapter (see events)")
        return

    recent = r.db.execute(
        "SELECT is_error, timed_out, exit_code, verdict FROM iterations WHERE lane=? AND ended_at IS NOT NULL"
        " ORDER BY id DESC LIMIT ?", (lane_id, max(3, int(r.budget()["stall_k"]))),
    ).fetchall()
    errors = 0
    for row in recent:
        if row["is_error"] or row["timed_out"] or row["exit_code"]:
            errors += 1
        else:
            break
    if errors >= 3:
        retire_lane(r, lane_id, "errored", "3 failed iterations in a row")
        return
    if (wd / r.m.get("done_marker", "\0")).exists() if r.m.get("done_marker") else False:
        retire_lane(r, lane_id, "done", f"target done marker {r.m['done_marker']} present")
        return
    stall_k = int(r.budget()["stall_k"])
    streak = r.db.execute(
        "SELECT verdict FROM iterations WHERE lane=? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT ?",
        (lane_id, stall_k),
    ).fetchall()
    if len(streak) >= stall_k and all(row["verdict"] != "ACCEPTED" for row in streak):
        retire_lane(r, lane_id, "stalled", f"no ACCEPTED in the last {stall_k} iterations")


# ---------------------------------------------------------------------------------------------
# the orchestrator loop


def budget_reason(r: Run, started_total: int) -> str | None:
    b = r.budget()
    t = r.totals()
    if t["cost"] >= b["max_usd"]:
        return f"budget: ${t['cost']:.2f} spent of ${b['max_usd']}"
    if started_total >= b["max_iterations"]:
        return f"budget: {started_total} iterations of {b['max_iterations']}"
    started = dt.datetime.fromisoformat(r.get("run_started_at", now()))
    hours = (dt.datetime.now() - started).total_seconds() / 3600
    if hours >= b["max_hours"]:
        return f"budget: {hours:.1f}h of {b['max_hours']}h"
    return None


def fill_lanes(r: Run):
    target = int(r.get("target_lanes"))
    active = r.db.execute("SELECT * FROM lanes WHERE state='active' ORDER BY seq").fetchall()
    failures = r.db.execute("SELECT COUNT(*) AS c FROM lanes WHERE state IN ('errored','setup_failed')").fetchone()["c"]
    if failures >= max(3, target):
        r.set("halt", f"{failures} lanes errored or failed setup; claude or the target setup is broken")
        return
    while len(active) < target:
        retired = r.db.execute(
            "SELECT * FROM lanes WHERE state IN ('stalled','done') AND id NOT IN"
            " (SELECT parent_lane FROM lanes WHERE parent_lane IS NOT NULL) ORDER BY ended_at LIMIT 1"
        ).fetchone()
        best = r.global_best()
        if retired is not None:
            start = best["commit_after"] if best else r.m["baseline_commit"]
            theme_id = pick_theme(r, retired["theme_id"])
            lane_id = create_lane(r, theme_id, start, retired["id"])
        else:
            theme_id = pick_theme(r, None)
            lane_id = create_lane(r, theme_id, r.m["baseline_commit"], None)
        if lane_id is None:
            return  # setup failure recorded; the failure count above decides whether to halt
        active = r.db.execute("SELECT * FROM lanes WHERE state='active' ORDER BY seq").fetchall()


def close_interrupted(r: Run, note: str):
    """Close iterations left open (crash, kill). They still pay the conservative trial charge, so an
    interruption can never shrink the multiple-testing correction. Cost is unknown and recorded as 0."""
    estimate = int(r.m["trials"]["per_iteration_estimate"])
    cur = r.db.execute(
        "UPDATE iterations SET ended_at=?, verdict='NONE', is_error=1, trials=?, trials_exact=0,"
        " cost_usd=COALESCE(cost_usd, 0), note=? WHERE ended_at IS NULL",
        (now(), estimate, note + "; cost unknown, trials charged at the estimate"),
    )
    r.db.commit()
    if cur.rowcount:
        r.event("interrupted", f"{cur.rowcount} open iteration(s) closed: {note}")


def cmd_run(args):
    r = Run(args.run_dir)
    pid_file = r.dir / "orchestrator.pid"
    if pid_file.exists() and pid_alive(int(pid_file.read_text().strip() or 0)) and \
            int(pid_file.read_text()) != os.getpid():
        raise SwarmError(f"orchestrator already running (pid {pid_file.read_text().strip()})")
    halt = r.get("halt")
    if halt and not args.clear_halt:
        raise SwarmError(f"swarm is halted: {halt}\nResolve it, then relaunch with --clear-halt (a human decision).")
    if halt:
        r.event("halt_cleared", f"cleared by human: {halt}")
        r.set("halt", None)
    pid_file.write_text(str(os.getpid()))
    if r.get("run_started_at") is None:
        r.set("run_started_at", now())
    running: dict = {}
    stop_now = {"flag": False}

    def on_term(signum, frame):
        stop_now["flag"] = True
    signal.signal(signal.SIGTERM, on_term)

    # iterations left open by a crashed orchestrator are closed as interrupted
    close_interrupted(r, "orchestrator restarted mid-iteration")
    r.event("start", f"phase={r.get('phase')} lanes={r.get('target_lanes')} pid={os.getpid()}")
    stopping = None
    try:
        try:
            stopping = orchestrate(r, running, stop_now)
        except Exception as exc:  # anything unexpected must halt loudly, never vanish
            for info in running.values():
                if info["proc"].poll() is None:
                    kill_lane(info["proc"], signal.SIGTERM)
            r.set("halt", f"orchestrator crashed: {type(exc).__name__}: {exc}")
            r.event("crashed", traceback.format_exc())
            close_interrupted(r, "orchestrator crashed mid-iteration")
            stopping = f"HALT: orchestrator crashed: {exc}"
    finally:
        pid_file.unlink(missing_ok=True)
        (r.dir / "STOP").unlink(missing_ok=True)
    return finish_run(r, stopping)


def orchestrate(r: Run, running: dict, stop_now: dict) -> str:
    stopping = None
    while True:
        bad = check_control_locks(r)
        if bad:
            r.set("halt", "swarm control files changed: " + ", ".join(bad))
        if stop_now["flag"]:
            for info in running.values():
                kill_lane(info["proc"], signal.SIGTERM)
            stopping = stopping or "stop --now"

        for lane_id, info in list(running.items()):
            timeout = float(r.m.get("iteration_timeout_min", 120)) * 60
            timed_out = False
            if info["proc"].poll() is None and time.time() - info["started"] > timeout:
                kill_lane(info["proc"], signal.SIGKILL)
                info["proc"].wait()
                timed_out = True
            if info["proc"].poll() is not None:
                del running[lane_id]
                finalize_iteration(r, lane_id, info, timed_out)

        started_total = r.db.execute("SELECT COUNT(*) AS c FROM iterations").fetchone()["c"]
        halt = r.get("halt")
        if halt:
            stopping = stopping or f"HALT: {halt}"
        if (r.dir / "STOP").exists():
            stopping = stopping or "stop requested"
        if not stopping:
            stopping = budget_reason(r, started_total)
        if not stopping and r.get("phase") == "pilot" and started_total >= r.m["pilot"]["iterations"]:
            stopping = "pilot_complete"
        if stopping:
            if not running:
                break
        else:
            fill_lanes(r)
            if r.get("halt"):
                continue
            for lane in r.db.execute("SELECT * FROM lanes WHERE state='active' ORDER BY seq").fetchall():
                if lane["id"] in running:
                    continue
                started_total = r.db.execute("SELECT COUNT(*) AS c FROM iterations").fetchone()["c"]
                if r.get("phase") == "pilot" and started_total >= r.m["pilot"]["iterations"]:
                    break
                if budget_reason(r, started_total):
                    break
                launch_iteration(r, lane, running)
        time.sleep(POLL_SECONDS)
    return stopping


def finish_run(r: Run, stopping: str):
    if stopping == "pilot_complete":
        r.set("phase", "pilot_done")
        report = pilot_report(r)
        (r.dir / "pilot_report.md").write_text(report)
        r.event("pilot_complete", f"pilot report: {r.dir / 'pilot_report.md'}")
    else:
        r.event("stopped", stopping)
    notify(f"{r.run_id}: {stopping}")
    return 0 if stopping in ("pilot_complete", "stop requested") or str(stopping).startswith("budget") else 1


def cmd_start(args):
    r = Run(args.run_dir)
    pid_file = r.dir / "orchestrator.pid"
    if pid_file.exists() and pid_alive(int(pid_file.read_text().strip() or 0)):
        raise SwarmError(f"already running (pid {pid_file.read_text().strip()})")
    if r.get("phase") == "pilot_done":
        raise SwarmError("pilot is complete; approve the full run with `scale` first")
    if r.get("halt") and not args.clear_halt:
        raise SwarmError(f"swarm is halted: {r.get('halt')}\nResolve it, then start with --clear-halt (a human decision).")
    if not shutil.which("claude"):
        raise SwarmError("claude CLI not on PATH")
    cmd = [sys.executable, str(Path(__file__).resolve()), "run", str(r.dir)]
    if args.clear_halt:
        cmd.append("--clear-halt")
    if shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i", *cmd]
    with open(r.dir / "orchestrator.out", "a") as out:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(50):
        if pid_file.exists():
            break
        time.sleep(0.2)
    if not pid_file.exists():
        raise SwarmError(f"orchestrator did not start; see {r.dir / 'orchestrator.out'}")
    print(f"started pid {pid_file.read_text().strip()}  log: {r.dir / 'orchestrator.log'}")


def cmd_stop(args):
    r = Run(args.run_dir)
    pid_file = r.dir / "orchestrator.pid"
    if not pid_file.exists() or not pid_alive(int(pid_file.read_text().strip() or 0)):
        print("not running")
        return
    if args.now:
        os.kill(int(pid_file.read_text()), signal.SIGTERM)
        print("sent SIGTERM: running iterations are being killed")
    else:
        (r.dir / "STOP").touch()
        print("STOP requested: running iterations will finish, then the orchestrator exits")


def cmd_scale(args):
    r = Run(args.run_dir)
    if r.get("phase") not in ("pilot_done", "full"):
        raise SwarmError(f"scale needs a completed pilot (phase is {r.get('phase')})")
    if args.lanes > r.m["lanes"]["max"]:
        raise SwarmError(f"--lanes {args.lanes} exceeds the approved lanes.max {r.m['lanes']['max']}")
    override = dict(r.get("budget_override", {}))
    if args.max_usd is not None:
        override["max_usd"] = args.max_usd
    if args.max_iterations is not None:
        override["max_iterations"] = args.max_iterations
    if args.max_hours is not None:
        override["max_hours"] = args.max_hours
    r.set("budget_override", override)
    r.set("full_model", args.model)
    r.set("target_lanes", args.lanes)
    r.set("phase", "full")
    r.event("scaled", f"lanes={args.lanes} model={args.model} budget_override={override}")
    print(f"approved full run: {args.lanes} lanes, new lanes on {args.model}. Start it with: start {r.dir}")


# ---------------------------------------------------------------------------------------------
# reporting


def hypothesis_clusters(texts: list) -> int:
    def tokens(s):
        return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}
    reps: list = []
    for t in texts:
        tk = tokens(t)
        if not tk:
            continue
        if not any(len(tk & rep) / len(tk | rep) >= 0.6 for rep in reps):
            reps.append(tk)
    return len(reps)


def pilot_report(r: Run) -> str:
    rows = r.db.execute("SELECT * FROM iterations WHERE ended_at IS NOT NULL").fetchall()
    th = {"distinct_min": 0.7, "rejected_max": 0.2, "accepted_min": 1}
    th.update(r.m.get("pilot", {}).get("thresholds", {}))
    n = len(rows) or 1
    cost = sum(x["cost_usd"] or 0 for x in rows)
    accepted = sum(1 for x in rows if x["verdict"] == "ACCEPTED")
    rejected = sum(1 for x in rows if x["verdict"] == "REJECTED")
    hyps = [x["hypothesis"] for x in rows if x["hypothesis"]]
    distinct = hypothesis_clusters(hyps) / len(hyps) if hyps else 0.0
    guard = r.db.execute(
        "SELECT kind, COUNT(*) AS c FROM events WHERE kind IN"
        " ('lane_quarantined','lane_errored','adapter_error','setup_failed','lane_stalled') GROUP BY kind"
    ).fetchall()
    hard_trips = sum(g["c"] for g in guard if g["kind"] in ("lane_quarantined", "adapter_error", "setup_failed"))
    b = r.m["budget"]
    projected = cost / n * b["max_iterations"]
    lines = [
        f"# Pilot report: {r.run_id}", "",
        f"Iterations: {len(rows)}  |  cost ${cost:.2f} (${cost / n:.2f}/iteration)  |"
        f" trials counted: {sum(x['trials'] or 0 for x in rows)}", "",
        "| Criterion | Value | Threshold | Result |", "|---|---|---|---|",
        f"| Projected cost for {b['max_iterations']} iterations | ${projected:.0f} | <= ${b['max_usd']} |"
        f" {'PASS' if projected <= b['max_usd'] else 'FAIL'} |",
        f"| Distinct hypotheses | {distinct:.0%} | >= {th['distinct_min']:.0%} |"
        f" {'PASS' if distinct >= th['distinct_min'] else 'FAIL'} |",
        f"| ACCEPTED iterations | {accepted} | >= {th['accepted_min']} |"
        f" {'PASS' if accepted >= th['accepted_min'] else 'FAIL'} |",
        f"| REJECTED rate | {rejected / n:.0%} | < {th['rejected_max']:.0%} |"
        f" {'PASS' if rejected / n < th['rejected_max'] else 'FAIL'} |",
        f"| Guard trips (tamper, adapter, setup) | {hard_trips} | 0 | {'PASS' if hard_trips == 0 else 'FAIL'} |",
        "", "## By model", "", "| Model | Iterations | $/iteration | Accept rate |", "|---|---|---|---|",
    ]
    for model in sorted({x["model"] for x in rows}):
        mr = [x for x in rows if x["model"] == model]
        acc = sum(1 for x in mr if x["verdict"] == "ACCEPTED")
        lines.append(f"| {model} | {len(mr)} | ${sum(x['cost_usd'] or 0 for x in mr) / len(mr):.2f} |"
                     f" {acc / len(mr):.0%} |")
    lines += ["", "## Events", ""] + [f"- {g['kind']}: {g['c']}" for g in guard]
    best = r.global_best()
    lines += ["", f"Global best: {dict(best) if best else 'none'}", "",
              "Decide: scale with `scale <run-dir> --lanes N --model <winner>`, or stop here."]
    return "\n".join(lines) + "\n"


def cmd_status(args):
    r = Run(args.run_dir)
    pid_file = r.dir / "orchestrator.pid"
    alive = pid_file.exists() and pid_alive(int(pid_file.read_text().strip() or 0))
    t = r.totals()
    b = r.budget()
    print(f"run {r.run_id}  phase={r.get('phase')}  orchestrator={'RUNNING' if alive else 'not running'}"
          f"  halt={r.get('halt')}")
    print(f"iterations {t['n']}/{b['max_iterations']}  cost ${t['cost']:.2f}/${b['max_usd']}  "
          f"trials {t['trials']}  running {t['running'] or 0}  target lanes {r.get('target_lanes')}")
    print("\nlane  state         theme            model   iters  accepted  best")
    for lane in r.db.execute("SELECT * FROM lanes ORDER BY seq"):
        s = r.db.execute(
            "SELECT COUNT(*) AS n, SUM(verdict='ACCEPTED') AS a, "
            + ("MAX" if r.maximize else "MIN") + "(CASE WHEN verdict='ACCEPTED' THEN score END) AS best"
            " FROM iterations WHERE lane=?", (lane["id"],)
        ).fetchone()
        print(f"{lane['id']:<5} {lane['state']:<13} {lane['theme_id'][:16]:<16} {lane['model'][:7]:<7}"
              f" {s['n']:>5}  {s['a'] or 0:>8}  {s['best'] if s['best'] is not None else '-'}")
    best = r.global_best()
    if best:
        print(f"\nglobal best: {best['score']} lane {best['lane']} commit {best['commit_after'][:12]}"
              f" :: {best['hypothesis']}")
    print("\nrecent events:")
    for e in r.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 8").fetchall()[::-1]:
        print(f"  {e['at']} {e['kind']:<16} {e['lane'] or '':<4} {(e['detail'] or '')[:100]}")


def cmd_registry(args):
    r = Run(args.run_dir)
    rows = r.db.execute(
        "SELECT i.lane, l.theme_id, i.verdict, i.score, i.hypothesis FROM iterations i JOIN lanes l ON l.id=i.lane"
        " WHERE i.hypothesis IS NOT NULL ORDER BY i.id DESC LIMIT ?", (args.limit,)
    ).fetchall()
    if not rows:
        print("(no hypotheses recorded yet)")
    for x in rows[::-1]:
        print(f"{x['lane']} [{x['theme_id']}] {x['verdict'] or 'RUNNING'} score={x['score']} :: {x['hypothesis']}")


# ---------------------------------------------------------------------------------------------
# harvest


def deflated_sharpe(returns: list, n_trials: int) -> dict:
    """Deflated Sharpe ratio (Bailey & Lopez de Prado, 2014) on per-period returns.

    The expected maximum Sharpe of n_trials under the null uses Var[SR] ~= 1/T, the sampling variance of
    a Sharpe estimate when every trial's true Sharpe is zero."""
    T = len(returns)
    if T < 30:
        raise SwarmError(f"need at least 30 return observations for DSR, got {T}")
    mean = statistics.fmean(returns)
    sd = statistics.pstdev(returns)
    if sd == 0:
        raise SwarmError("returns have zero variance; Sharpe undefined")
    sr = mean / sd
    skew = sum((x - mean) ** 3 for x in returns) / T / sd ** 3
    kurt = sum((x - mean) ** 4 for x in returns) / T / sd ** 4
    nd = statistics.NormalDist()
    gamma = 0.5772156649015329
    n = max(int(n_trials), 2)
    sr0 = math.sqrt(1.0 / T) * ((1 - gamma) * nd.inv_cdf(1 - 1.0 / n) + gamma * nd.inv_cdf(1 - 1.0 / (n * math.e)))
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        raise SwarmError("DSR denominator non-positive (extreme skew/kurtosis)")
    dsr = nd.cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(denom))
    return {"sr": sr, "sr0": sr0, "dsr": dsr, "T": T, "skew": skew, "kurtosis": kurt}


def temp_worktree(r: Run, commit: str, name: str) -> Path:
    path = r.lanes_root / "_harvest" / name
    if path.exists():
        git(r.repo, "worktree", "remove", "--force", str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    git(r.repo, "worktree", "add", "-q", "--detach", str(path), commit)
    return path


def adapter_json(r: Run, template: str, wt: Path, commit: str, extra: dict, env_extra: dict):
    wd = r.workdir(wt)
    values = {"run_dir": r.dir, "lane_dir": wt, "workdir": wd, "commit": commit, **extra}
    env = lane_base_env(r)
    env.update(env_extra)
    env.update({"SWARM_LANE_DIR": str(wt), "SWARM_WORKDIR": str(wd), "SWARM_COMMIT": commit,
                **{f"SWARM_{k.upper()}": str(v) for k, v in extra.items()}})
    proc = run(render(template, values), cwd=wd, env=env, check=False,
               timeout=float(r.m.get("harvest", {}).get("timeout_min", 180)) * 60)
    if proc.returncode != 0:
        raise SwarmError(f"harvest adapter exited {proc.returncode}: {proc.stderr.strip()[-400:]}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise SwarmError(f"harvest adapter printed no JSON on its last line: {proc.stdout[-300:]!r}") from exc


def cmd_harvest(args):
    r = Run(args.run_dir)
    pid_file = r.dir / "orchestrator.pid"
    if pid_file.exists() and pid_alive(int(pid_file.read_text().strip() or 0)):
        raise SwarmError("orchestrator is running; stop it before harvesting")
    h = {"shortlist": 5, "finalists": 3, "seeds": 3, "min_ratio": 0.5, "dsr_min": 0.95}
    h.update(r.m.get("harvest", {}))
    order = "DESC" if r.maximize else "ASC"
    per_lane = r.db.execute(
        "SELECT * FROM iterations WHERE verdict='ACCEPTED' AND score IS NOT NULL AND commit_after != commit_before"
        f" ORDER BY score {order}"
    ).fetchall()
    seen, shortlist = set(), []
    for row in per_lane:
        if row["lane"] in seen:
            continue
        seen.add(row["lane"])
        shortlist.append(row)
        if len(shortlist) >= h["shortlist"]:
            break
    t = r.totals()
    n_trials = int(t["trials"])
    exact = r.db.execute("SELECT MIN(trials_exact) AS e FROM iterations WHERE ended_at IS NOT NULL").fetchone()["e"]
    report = [f"# Harvest: {r.run_id}", "",
              f"Iterations {t['n']}, cost ${t['cost']:.2f}, trials counted for correction: {n_trials}"
              f" ({'exact' if exact else 'includes conservative estimates'}).", ""]
    if not r.m.get("returns_command") and not r.m.get("reeval_command"):
        report += ["> **NO MULTIPLE-TESTING CORRECTION APPLIED.** The target provides neither a returns"
                   " command (for the deflated Sharpe ratio) nor a re-evaluation command (for the stability"
                   " re-check). Treat every score below as optimistic.", ""]
    results = []
    for k, row in enumerate(shortlist, 1):
        entry = {"iteration_id": row["id"], "lane": row["lane"], "commit": row["commit_after"], "score": row["score"],
                 "hypothesis": row["hypothesis"], "stability": None, "dsr": None, "passed": True, "notes": []}
        lane_env = json.loads(r.db.execute("SELECT env_json FROM lanes WHERE id=?", (row["lane"],)).fetchone()[0])
        wt = temp_worktree(r, row["commit_after"], f"c{k}")
        try:
            if r.m.get("reeval_command"):
                scores = []
                for seed in range(1, int(h["seeds"]) + 1):
                    res = adapter_json(r, r.m["reeval_command"], wt, row["commit_after"], {"seed": seed}, lane_env)
                    scores.append(float(res["score"]))
                med = statistics.median(scores)
                orig = float(row["score"])
                slack = abs(orig) * (1 - float(h["min_ratio"]))
                ok = (med >= orig - slack) if r.maximize else (med <= orig + slack)
                entry["stability"] = {"scores": scores, "median": med, "passed": ok}
                if not ok:
                    entry["passed"] = False
                    entry["notes"].append(f"unstable: median re-eval {med:.4g} vs original {orig:.4g}")
            if r.m.get("returns_command"):
                res = adapter_json(r, r.m["returns_command"], wt, row["commit_after"], {}, lane_env)
                returns = [float(x) for x in res["returns"]]
                d = deflated_sharpe(returns, n_trials)
                d["passed"] = d["dsr"] >= float(h["dsr_min"])
                entry["dsr"] = d
                if not d["passed"]:
                    entry["passed"] = False
                    entry["notes"].append(f"DSR {d['dsr']:.3f} < {h['dsr_min']} (N={n_trials})")
        except (SwarmError, KeyError, TypeError, ValueError, statistics.StatisticsError) as exc:
            entry["passed"] = False
            entry["notes"].append(f"harvest check failed: {type(exc).__name__}: {exc}")
        finally:
            git(r.repo, "worktree", "remove", "--force", str(wt), check=False)
            if wt.exists():
                r.event("cleanup_warning", f"could not remove harvest worktree {wt}")
        results.append(entry)

    finalists = [e for e in results if e["passed"]][: int(h["finalists"])]
    report += ["## Shortlist (best ACCEPTED per lane)", "",
               "| # | Lane | Score | Stability | DSR | Result | Hypothesis |", "|---|---|---|---|---|---|---|"]
    for k, e in enumerate(results, 1):
        stab = "n/a" if e["stability"] is None else f"median {e['stability']['median']:.4g}"
        dsr = "n/a" if e["dsr"] is None else f"{e['dsr']['dsr']:.3f}"
        verdict = "PASS" if e["passed"] else "FAIL: " + "; ".join(e["notes"])
        report.append(f"| {k} | {e['lane']} | {e['score']:.4g} | {stab} | {dsr} | {verdict} | {e['hypothesis'] or ''} |")
    report += ["", "## Finalists and lockbox", ""]
    if not finalists:
        report += ["No candidate survived the checks. The swarm found nothing that holds up; that is a result,"
                   " not a failure to report around."]
    for k, e in enumerate(finalists, 1):
        wt = r.lanes_root / f"finalist-{k}"
        if not wt.exists():
            git(r.repo, "worktree", "add", "-q", "--detach", str(wt), e["commit"])
        e["finalist"] = k
        e["worktree"] = str(wt)
        cmd = render(r.m["lockbox_command"], {"run_dir": r.dir, "lane_dir": wt, "workdir": r.workdir(wt),
                                             "commit": e["commit"]})
        report += [f"### Finalist {k}: lane {e['lane']}, score {e['score']:.4g}, commit {e['commit'][:12]}", "",
                   f"Hypothesis: {e['hypothesis']}", "",
                   "Run this yourself, once. The swarm never runs it:", "",
                   f"```bash\ncd {r.workdir(wt)}\n{cmd}\n```", "",
                   f"Then record it: `python3 {Path(__file__).resolve()} record-lockbox {r.dir} {k}"
                   " --score <score> --passed yes|no`", ""]
    r.set("harvest", {"at": now(), "n_trials": n_trials, "results": results, "finalists": finalists})
    (r.dir / "harvest.md").write_text("\n".join(report) + "\n")
    print("\n".join(report))


def cmd_record_lockbox(args):
    r = Run(args.run_dir)
    harvest = r.get("harvest")
    if not harvest:
        raise SwarmError("no harvest recorded; run harvest first")
    fin = next((f for f in harvest["finalists"] if f["finalist"] == args.finalist), None)
    if fin is None:
        raise SwarmError(f"no finalist {args.finalist}")
    if r.db.execute("SELECT 1 FROM lockbox WHERE finalist=?", (args.finalist,)).fetchone():
        raise SwarmError(f"finalist {args.finalist} already has a lockbox result; the lockbox is one-shot")
    r.db.execute(
        "INSERT INTO lockbox(finalist, iteration_id, commit_sha, score, passed, at, note) VALUES(?,?,?,?,?,?,?)",
        (args.finalist, fin["iteration_id"], fin["commit"], args.score, int(args.passed == "yes"), now(), args.note),
    )
    r.db.commit()
    r.event("lockbox", f"finalist {args.finalist} score={args.score} passed={args.passed}")
    print("recorded")


def cmd_relock(args):
    """Accept intentional changes to swarm control files (skill upgrade, adapter fix). Human decision."""
    r = Run(args.run_dir)
    pid_file = r.dir / "orchestrator.pid"
    if pid_file.exists() and pid_alive(int(pid_file.read_text().strip() or 0)):
        raise SwarmError("orchestrator is running; stop it first")
    locks = json.loads((r.dir / "locks.json").read_text())
    changed = check_control_locks(r)
    locks["control"] = {str(p): sha256_file(p) for p in control_files(r)}
    (r.dir / "locks.json").write_text(json.dumps(locks, indent=2))
    r.event("relocked", f"control files re-hashed by human; changed: {', '.join(changed) or 'none'}")
    print(f"relocked; changed files accepted: {', '.join(changed) or 'none'}")
    print("target locked files are unchanged. Clear any halt with: start <run-dir> --clear-halt")


def cmd_cleanup(args):
    r = Run(args.run_dir)
    pid_file = r.dir / "orchestrator.pid"
    if pid_file.exists() and pid_alive(int(pid_file.read_text().strip() or 0)):
        raise SwarmError("orchestrator is running; stop it first")
    harvest = r.get("harvest") or {"finalists": []}
    keep = {f["lane"] for f in harvest["finalists"]}
    lanes = r.db.execute("SELECT * FROM lanes ORDER BY seq").fetchall()
    for lane in lanes:
        wt = Path(lane["worktree"])
        if lane["id"] in keep:
            print(f"keep   {lane['id']} (finalist lane) {wt}")
            continue
        tag = f"swarm-archive/{r.run_id}/{lane['id']}"
        print(f"{'remove' if args.confirm else 'would remove'} {lane['id']} worktree {wt}; branch {lane['branch']}"
              f" -> tag {tag}")
        if not args.confirm:
            continue
        if not str(wt).startswith(str(r.lanes_root)) or not lane["branch"].startswith(f"swarm/{r.run_id}/"):
            raise SwarmError(f"refusing to touch {wt} / {lane['branch']}: outside this run")
        if git(r.repo, "rev-parse", "--verify", "--quiet", lane["branch"], check=False):
            git(r.repo, "tag", "-f", tag, lane["branch"])
        if wt.exists():
            git(r.repo, "worktree", "remove", "--force", str(wt))
        deleted = run(["git", "-C", str(r.repo), "branch", "-D", lane["branch"]], check=False)
        if deleted.returncode != 0:
            r.event("cleanup_warning", f"branch {lane['branch']} not deleted: {deleted.stderr.strip()}", lane["id"])
        if lane["state"] == "active":
            retire_lane(r, lane["id"], "archived", f"worktree removed by cleanup; history in tag {tag}")
        else:
            r.db.execute("UPDATE lanes SET state='archived' WHERE id=?", (lane["id"],))
            r.db.commit()
    if not args.confirm:
        print("\ndry run; re-run with --confirm to apply")


# ---------------------------------------------------------------------------------------------


def main(argv=None):
    p = argparse.ArgumentParser(prog="swarm.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init"); s.add_argument("draft"); s.set_defaults(fn=cmd_init)
    for name, fn in (("start", cmd_start), ("run", cmd_run)):
        s = sub.add_parser(name); s.add_argument("run_dir"); s.add_argument("--clear-halt", action="store_true")
        s.set_defaults(fn=fn)
    for name, fn in (("status", cmd_status), ("harvest", cmd_harvest)):
        s = sub.add_parser(name); s.add_argument("run_dir"); s.set_defaults(fn=fn)
    s = sub.add_parser("stop"); s.add_argument("run_dir"); s.add_argument("--now", action="store_true")
    s.set_defaults(fn=cmd_stop)
    s = sub.add_parser("scale"); s.add_argument("run_dir"); s.add_argument("--lanes", type=int, required=True)
    s.add_argument("--model", required=True); s.add_argument("--max-usd", type=float)
    s.add_argument("--max-iterations", type=int); s.add_argument("--max-hours", type=float)
    s.set_defaults(fn=cmd_scale)
    s = sub.add_parser("registry"); s.add_argument("run_dir"); s.add_argument("--limit", type=int, default=60)
    s.set_defaults(fn=cmd_registry)
    s = sub.add_parser("record-lockbox"); s.add_argument("run_dir"); s.add_argument("finalist", type=int)
    s.add_argument("--score", type=float, required=True); s.add_argument("--passed", choices=["yes", "no"], required=True)
    s.add_argument("--note"); s.set_defaults(fn=cmd_record_lockbox)
    s = sub.add_parser("relock"); s.add_argument("run_dir"); s.set_defaults(fn=cmd_relock)
    s = sub.add_parser("cleanup"); s.add_argument("run_dir"); s.add_argument("--confirm", action="store_true")
    s.set_defaults(fn=cmd_cleanup)
    args = p.parse_args(argv)
    try:
        return args.fn(args) or 0
    except SwarmError as exc:
        print(f"swarm.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
