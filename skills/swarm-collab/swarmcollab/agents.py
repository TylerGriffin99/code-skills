"""Role agent launch layer: env/cmd construction, detached launch, and drop ingestion.

Mirrors the shape of swarm-loop's swarm.py (`lane_base_env`, `launch_iteration`)
but is standalone: it does not import swarm.py, and every agent process is a
role (strategist/implementor/critic) rather than a lane.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from swarmcollab import board
from swarmcollab.util import CollabError, git, now, render
from swarmcollab.views import estimate_tokens, render_view

# swarmcollab/agents.py -> swarmcollab/ -> skill root (sibling of tests/, roles/)
_PACKAGE_DIR = Path(__file__).resolve().parent
SKILL_DIR = _PACKAGE_DIR.parent

ROLE_PREAMBLE = {
    "strategist": "You are the strategist. Review the board and post proposals or messages.",
    "implementor": "You are an implementor. Pursue your assigned proposal and report a result.",
    "critic": "You are the critic. Critique uncritiqued proposals and results.",
}

# Vars that configure the process substituted via COLLAB_AGENT_CMD (the launch
# seam used only when overriding the agent command, e.g. in tests). A
# substituted command's own config (e.g. the mock agent's COLLAB_MOCK_SCRIPT)
# must still reach its env, so launch_turn re-forwards these two by name —
# but ONLY when COLLAB_AGENT_CMD is set. See base_env()'s docstring: in a
# production run (no override), the COLLAB_* strip stays absolute.
_SEAM_ENV_VARS = ("COLLAB_AGENT_CMD", "COLLAB_MOCK_SCRIPT")


def base_env(r) -> dict:
    """A copy of the process environment with `strip_env` and every COLLAB_* var removed.

    This strip is unconditional and total: every var starting with `COLLAB_`
    is removed, with no exceptions made here. `launch_turn` always re-adds
    `COLLAB_DROP` (and `COLLAB_RUN_DIR`) on top of this, and separately
    re-forwards `COLLAB_AGENT_CMD`/`COLLAB_MOCK_SCRIPT` — but only when
    `COLLAB_AGENT_CMD` is set (the agent-command override / test seam). A
    production run (no override) gets nothing back except what `launch_turn`
    itself computes.
    """
    env = dict(os.environ)
    for key in r.m.get("strip_env", []):
        env.pop(key, None)
    for key in list(env):
        if key.startswith("COLLAB_"):
            env.pop(key)
    return env


def build_cmd(r, role: str, model: str) -> list:
    """The `claude -p` invocation for a role's turn.

    `COLLAB_AGENT_CMD`, when set, is the seam tests use to substitute a mock
    agent in place of the real `claude` binary: it is returned split, verbatim.
    """
    override = os.environ.get("COLLAB_AGENT_CMD")
    if override:
        return shlex.split(override)
    return [
        "claude", "-p", "--output-format", "json",
        "--permission-mode", r.m.get("permission_mode", "auto"),
        "--model", model,
        "--disallowedTools", "Bash(git push:*)", "Bash(gh:*)",
    ]


def _drop_file_name(role: str, agent: str, round: int) -> str:
    return f"{role}-{agent}-r{round}.jsonl"


def _log_stem(r, role: str, agent: str, round: int) -> Path:
    """The stdout(`.json`)/stderr(`.err`) log stem for a turn, shared by launch + reap."""
    return r.dir / "logs" / f"{role}-{agent}-r{round}"


def _build_prompt(r, *, role: str, agent: str, round: int) -> str:
    """The real turn prompt: the rendered role contract from `roles/{role}.md`.

    The role file carries the drop-file contract (`$COLLAB_DROP`), the envelope
    shape, and the message quota the agent must follow, so a real run must send
    it — the built-in one-line `ROLE_PREAMBLE` teaches none of that and is only a
    fallback for when the role file is absent.
    """
    view = render_view(r, role=role, agent=agent, round=round)
    role_file = SKILL_DIR / "roles" / f"{role}.md"
    if role_file.is_file():
        return render(
            role_file.read_text(),
            {"goal": r.m.get("goal", ""), "agent": agent, "round": round, "view": view},
        )
    preamble = ROLE_PREAMBLE.get(role, f"You are the {role}.")
    return preamble + "\n\n" + view


def launch_turn(r, *, role: str, agent: str, model: str, round: int, cwd, drop_dir) -> subprocess.Popen:
    """Launch one role agent's turn as a detached subprocess and record its turns row.

    Writes the rendered board view (prefixed with a role preamble) to a prompt
    file fed on stdin, points COLLAB_DROP at a per-turn drop file the agent
    (or the mock) writes its intended board entries to, and launches the
    process detached (`start_new_session=True`) so the orchestrator can poll
    it without holding it open.
    """
    cwd = Path(cwd)
    drop_dir = Path(drop_dir)
    drop_dir.mkdir(parents=True, exist_ok=True)
    drop_file = drop_dir / _drop_file_name(role, agent, round)

    prompt = _build_prompt(r, role=role, agent=agent, round=round)

    prompts_dir = r.dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompts_dir / f"{role}-{agent}-r{round}.prompt.md"
    prompt_path.write_text(prompt)

    env = base_env(r)
    # base_env() strips ALL COLLAB_* unconditionally. In production (no
    # COLLAB_AGENT_CMD override) that strip stays absolute: nothing below is
    # re-added except COLLAB_DROP/COLLAB_RUN_DIR. Only when COLLAB_AGENT_CMD
    # is set (the test/override seam) do we re-forward the seam vars a
    # substituted command needs to read its own config from its env.
    if os.environ.get("COLLAB_AGENT_CMD"):
        for seam_var in _SEAM_ENV_VARS:
            if seam_var in os.environ:
                env[seam_var] = os.environ[seam_var]
    env["COLLAB_DROP"] = str(drop_file)
    env["COLLAB_RUN_DIR"] = str(r.dir)

    cmd = build_cmd(r, role, model)

    logs_dir = r.dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stem = _log_stem(r, role, agent, round)

    commit_before = git(cwd, "rev-parse", "HEAD")

    # Launch before recording the turn: if Popen itself raises (missing binary,
    # bad permissions on the log files, ...) we must not leave behind a "started"
    # turns row / event with no process backing it — a phantom turn that no
    # exit-code reaper could ever resolve.
    with open(prompt_path) as stdin, \
            open(stem.with_suffix(".json"), "w") as out, \
            open(stem.with_suffix(".err"), "w") as err:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env, stdin=stdin, stdout=out, stderr=err,
            start_new_session=True,
        )

    r.db.execute(
        "INSERT INTO turns(round, role, agent, model, started_at, worktree, commit_before)"
        " VALUES(?,?,?,?,?,?,?)",
        (round, role, agent, model, now(), str(cwd), commit_before),
    )
    r.db.commit()
    r.event("turn_started", f"round={round} model={model}", agent)
    return proc


def _parse_usage(text: str):
    """Best-effort parse of a `claude -p --output-format json` summary log.

    Returns the decoded dict, or None if the file holds no JSON object. Tolerant
    of trailing/leading noise: falls back to the last non-empty line if the whole
    text is not itself a single JSON object.
    """
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
        return None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def reap_turn(r, *, role: str, agent: str, round: int, turn_id, returncode, timed_out: bool = False) -> dict:
    """Record a finished turn's outcome: ended_at, exit_code, cost, and tokens.

    Reads the turn's stdout summary log (`logs/{role}-{agent}-r{round}.json`,
    the shape `claude -p --output-format json` writes) for `total_cost_usd` and
    `usage.output_tokens`. A missing or malformed usage file is tolerated: what
    is present is recorded, an event is logged, and no exception is raised.

    A non-zero exit is a crashed agent, recorded distinctly (`turn_process_failed`
    event + `is_error=1`) so it is never confused with an agent that ran cleanly
    but chose to post nothing (which surfaces separately as `drop_missing`).
    """
    cost = tokens = None
    data = None
    log_path = _log_stem(r, role, agent, round).with_suffix(".json")
    try:
        if log_path.is_file():
            data = _parse_usage(log_path.read_text())
    except OSError as exc:
        r.event("turn_usage_missing", f"could not read usage log for {role}/{agent} round {round}: {exc}", agent)

    if data is None:
        r.event("turn_usage_missing", f"no parseable usage json for {role}/{agent} round {round}: {log_path}", agent)
    else:
        cost = _number(data.get("total_cost_usd"))
        usage = data.get("usage")
        if isinstance(usage, dict):
            tok = _number(usage.get("output_tokens"))
            tokens = int(tok) if tok is not None else None
        if cost is None or tokens is None:
            missing = " ".join(w for w, ok in (("cost", cost is None), ("tokens", tokens is None)) if ok)
            r.event("turn_usage_partial", f"usage json for {role}/{agent} round {round} missing {missing}", agent)

    failed = returncode is not None and returncode != 0
    is_error = 1 if (failed or timed_out) else None
    if turn_id is not None:
        # COALESCE keeps any value a fake/earlier step already wrote when this reap
        # has nothing to add (missing usage log), and never clears an is_error flag.
        r.db.execute(
            "UPDATE turns SET ended_at=?, exit_code=?, cost_usd=COALESCE(?, cost_usd),"
            " tokens=COALESCE(?, tokens), is_error=COALESCE(?, is_error) WHERE id=?",
            (now(), returncode, cost, tokens, is_error, turn_id),
        )
        r.db.commit()

    # A crashed process is distinct from a clean turn that posted nothing. A
    # timed-out turn is already surfaced by its own `turn_timed_out` event, so
    # it is not double-labelled here.
    if failed and not timed_out:
        r.event("turn_process_failed", f"{role}/{agent} round {round} exited {returncode}", agent)

    return {"cost": cost, "tokens": tokens, "exit_code": returncode}


def _message_text(body) -> str:
    if isinstance(body, dict):
        return str(body.get("text", ""))
    return str(body)


def ingest_drop(r, *, role: str, agent: str, round: int, drop_file) -> list:
    """Read an agent's JSONL drop file and `board.append` the accepted entries.

    `message` entries are quota-limited: at most `messaging.per_turn_quota`
    are accepted per call, and any whose body text estimates over
    `messaging.max_tokens` is rejected regardless of quota. Each rejected
    message logs a `message_dropped` event. Non-`message` entries are always
    accepted (not quota-limited).
    """
    drop_file = Path(drop_file)
    if not drop_file.is_file():
        r.event(
            "drop_missing",
            f"no drop file for {role}/{agent} round {round}: {drop_file}",
            agent,
        )
        return []

    quota = r.m["messaging"]["per_turn_quota"]
    max_tokens = r.m["messaging"]["max_tokens"]

    with open(drop_file) as fh:
        raw_lines = [line.strip() for line in fh if line.strip()]

    accepted = []
    accepted_messages = 0
    for line in raw_lines:
        # A single malformed line must never abort ingestion of the rest: a
        # truncated/undecodable line, a non-object, a line with no `type`, or one
        # whose `type` the board rejects is logged and skipped, the same spirit as
        # oversized/over-quota messages below.
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            r.event("drop_line_malformed", f"undecodable JSONL line from {role}/{agent} round {round}", agent)
            continue
        if not isinstance(item, dict) or "type" not in item:
            r.event("drop_line_malformed", f"line missing 'type' from {role}/{agent} round {round}", agent)
            continue
        entry_type = item["type"]
        body = item.get("body", {})
        refs = item.get("refs")
        to = item.get("to")

        if entry_type == "message":
            tokens = estimate_tokens(_message_text(body))
            if tokens > max_tokens:
                r.event(
                    "message_dropped",
                    f"oversized ({tokens} > {max_tokens} tokens) from {role}/{agent} round {round}",
                    agent,
                )
                continue
            if accepted_messages >= quota:
                r.event(
                    "message_dropped",
                    f"over per-turn quota ({quota}) from {role}/{agent} round {round}",
                    agent,
                )
                continue

        try:
            entry = board.append(
                r, round=round, role=role, author_agent=agent, type=entry_type,
                body=body, refs=refs, to=to,
            )
        except CollabError as exc:
            r.event(
                "drop_line_malformed",
                f"rejected entry ({entry_type!r}) from {role}/{agent} round {round}: {exc}",
                agent,
            )
            continue
        if entry_type == "message":
            accepted_messages += 1
        accepted.append(entry)

    return accepted
