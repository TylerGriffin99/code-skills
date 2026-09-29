"""Two-tier evaluation: a cheap proxy score gates a slower, authoritative full score.

Mirrors the shape of swarm-loop's swarm.py (`run_eval` ~628-652, `verify_accepted`
~704-731) but is standalone: it does not import swarm.py. The swarm runs the eval
commands itself, on the commit it checked out; an agent's own claimed score is never
trusted or recorded (see `score_result`).
"""

from __future__ import annotations

import json
import os

from swarmcollab import util
from swarmcollab.util import CollabError

EVAL_COMMAND_KEYS = ("proxy_command", "full_command")


def run_eval(r, command_key: str, worktree, commit, agent, extra=None) -> dict:
    """Run the locked `r.m["eval"][command_key]` command against `commit` in `worktree`.

    Swarm-run, never agent-run: the caller supplies `commit`/`worktree`, and the
    result is read only from the command's own last stdout line. Exports
    `COLLAB_COMMIT`, `COLLAB_WORKDIR`, `COLLAB_RUN_DIR`, and `COLLAB_AGENT`, plus
    one `COLLAB_<KEY>` var per `extra` entry (e.g. `extra={"round": 2}` exports
    `COLLAB_ROUND`). Raises `CollabError` if the command fails or its last stdout
    line is not JSON with a numeric `score`.
    """
    if command_key not in EVAL_COMMAND_KEYS:
        raise CollabError(f"unknown eval command_key: {command_key!r}")
    try:
        cmd = r.m["eval"][command_key]
    except KeyError:
        raise CollabError(f"manifest eval has no {command_key!r}") from None

    extra = extra or {}
    env = dict(os.environ)
    env["COLLAB_COMMIT"] = str(commit)
    env["COLLAB_WORKDIR"] = str(worktree)
    env["COLLAB_RUN_DIR"] = str(r.dir)
    env["COLLAB_AGENT"] = str(agent)
    for key, value in extra.items():
        env[f"COLLAB_{key.upper()}"] = str(value)

    timeout = float(r.m.get("eval_timeout_min", 120)) * 60
    proc = util.run(cmd, cwd=worktree, env=env, check=False, timeout=timeout)
    if proc.returncode != 0:
        raise CollabError(f"eval ({command_key}) exited {proc.returncode}: {proc.stderr.strip()[-400:]}")

    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise CollabError(
            f"eval ({command_key}) printed no JSON on its last line: {proc.stdout[-300:]!r}"
        ) from exc

    score = result.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise CollabError(f"eval ({command_key}) JSON has no numeric score: {result}")
    return result


def score_result(r, *, worktree, commit, agent, round, turn_id, board_entry_id=None) -> dict:
    """Score a turn's commit with the proxy eval and record it as authoritative.

    The recorded `proxy_score` comes only from `run_eval`'s locked command; any
    score an agent claims on the blackboard is never read here. When the proxy
    score clears `eval.promote_threshold` (or, absent a threshold, beats the best
    proxy_score recorded so far), the full eval also runs and its score is
    recorded with `promoted=1`. Returns `{"proxy_score": float, "promoted": bool}`.

    `board_entry_id`, when given, links this results row to the agent's own board
    `result` entry so shared views can surface the authoritative score for that
    entry instead of the agent's self-reported one (see honesty invariant).
    """
    ev = r.m["eval"]
    proxy_result = run_eval(r, "proxy_command", worktree, commit, agent, extra={"round": round})
    proxy_score = float(proxy_result["score"])

    maximize = bool(r.m.get("maximize", True))
    threshold = ev.get("promote_threshold")
    if threshold is not None:
        promote = proxy_score >= threshold if maximize else proxy_score <= threshold
    else:
        order = "MAX" if maximize else "MIN"
        best = r.db.execute(f"SELECT {order}(proxy_score) AS b FROM results").fetchone()["b"]
        promote = best is None or (proxy_score > best if maximize else proxy_score < best)

    cur = r.db.execute(
        "INSERT INTO results(turn_id, agent, round, proxy_score, commit_sha, board_entry_id, at)"
        " VALUES(?,?,?,?,?,?,?)",
        (turn_id, agent, round, proxy_score, commit, board_entry_id, util.now()),
    )
    result_id = cur.lastrowid
    r.db.commit()

    promoted = False
    if promote:
        full_result = run_eval(r, "full_command", worktree, commit, agent, extra={"round": round})
        full_score = float(full_result["score"])
        r.db.execute("UPDATE results SET full_score=?, promoted=1 WHERE id=?", (full_score, result_id))
        r.db.commit()
        promoted = True

    return {"proxy_score": proxy_score, "promoted": promoted}


def verify(r, *, worktree, commit, recorded: float) -> tuple[bool, float]:
    """Re-run the full eval on `commit` and compare it against a previously `recorded` score.

    Returns `(ok, new_score)`, where `ok` is whether `abs(new_score - recorded)`
    is within `eval.verify_tolerance`. The swarm's own re-scoring is authoritative;
    `recorded` is only ever a claim to be checked, never trusted outright.
    """
    result = run_eval(r, "full_command", worktree, commit, agent="verify")
    new_score = float(result["score"])
    tolerance = float(r.m["eval"].get("verify_tolerance", 0.0))
    ok = abs(new_score - recorded) <= tolerance
    return ok, new_score
