"""Two-tier evaluation: a cheap proxy score gates a slower, authoritative full score.

Mirrors the shape of swarm-loop's swarm.py (`run_eval` ~628-652, `verify_accepted`
~704-731) but is standalone: it does not import swarm.py. The swarm runs the eval
commands itself, on the commit it checked out; an agent's own claimed score is never
trusted or recorded (see `score_result`).
"""

from __future__ import annotations

import json
import math
import os

from swarmcollab import board
from swarmcollab import util
from swarmcollab.util import CollabError

EVAL_COMMAND_KEYS = ("proxy_command", "full_command")
#: Cap on the eval detail a `scored` entry carries, so one verbose eval cannot flood the board
METRICS_MAX_CHARS = 1500
#: Cap on the one-line metrics summary inside a `scored` entry's text
METRICS_LINE_MAX_CHARS = 220


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
    if not math.isfinite(score):
        raise CollabError(f"eval ({command_key}) score is not finite: {score}")
    return result


def _metrics(r, result: dict, agent: str) -> dict:
    """The eval's detail for the board: `eval.board_keys` if set, else every key but `score`.

    Keys are kept in order until the JSON would pass METRICS_MAX_CHARS; the rest are named in
    `_omitted` (and logged) so a reader knows the detail was cut, not missing. A configured
    board key the eval did not print is logged too, never skipped quietly.
    """
    configured = r.m["eval"].get("board_keys")
    keys = configured or [k for k in result if k != "score"]
    out, omitted, size = {}, [], 2
    missing = [k for k in keys if k not in result]
    for key in keys:
        if key not in result or key == "score":
            continue
        piece = len(json.dumps({key: result[key]}))
        if size + piece > METRICS_MAX_CHARS:
            omitted.append(key)
            continue
        out[key] = result[key]
        size += piece
    if omitted:
        out["_omitted"] = omitted
        r.event("metrics_truncated", f"{agent}: eval keys over {METRICS_MAX_CHARS} chars left off the board: "
                f"{', '.join(omitted)}", agent)
    if configured and missing:
        out["_missing"] = missing
        r.event("metrics_missing", f"{agent}: eval.board_keys not in the eval output: {', '.join(missing)}", agent)
    return out


def _metrics_line(metrics: dict) -> str:
    parts = [f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}" for k, v in metrics.items()
             if isinstance(v, (int, float, str)) and not isinstance(v, bool) and not k.startswith("_")]
    line = " ".join(parts)
    return line if len(line) <= METRICS_LINE_MAX_CHARS else line[: METRICS_LINE_MAX_CHARS - 1] + "…"


def best_score_before(r, kind: str):
    """The best `kind` ("proxy" or "full") score recorded so far, the baseline's included.

    Deltas compare like with like: a commit scored only by the proxy is compared with proxy
    scores, a promoted one with full scores, since the two evals can run on different data.
    """
    if kind not in ("proxy", "full"):
        raise CollabError(f"unknown score kind: {kind!r}")
    maximize = bool(r.m.get("maximize", True))
    column = "full_score" if kind == "full" else "proxy_score"
    scores = [row["s"] for row in r.db.execute(f"SELECT {column} AS s FROM results") if row["s"] is not None]
    baseline = (r.get("baseline_scores") or {}).get(kind)
    if isinstance(baseline, (int, float)):
        scores.append(baseline)
    if not scores:
        return None
    return max(scores) if maximize else min(scores)


def has_scored(r, agent: str, round: int) -> bool:
    """True if the orchestrator already posted a `scored` entry for `agent` in `round`."""
    return any(
        isinstance(e.get("body"), dict) and e["body"].get("agent") == agent and board.is_trusted(r, e)
        for e in board.read(r, types=["scored"], rounds=[round])
    )


def post_outcome_safely(r, **kwargs) -> None:
    """`post_scored` for an error path: a failure to post is logged, never raised over the
    error being handled (which the caller still reports or re-raises)."""
    try:
        post_scored(r, **kwargs)
    except Exception as exc:
        r.event("scored_post_failed", f"{kwargs.get('agent')} round {kwargs.get('round')} "
                f"{kwargs.get('status')}: could not post the outcome: {type(exc).__name__}: {exc}",
                kwargs.get("agent"))


def post_scored(r, *, agent, round, commit=None, proxy_result=None, full_result=None, board_entry_id=None,
                best_before=None, status="ok", error=None) -> dict:
    """Append the orchestrator's own `scored` entry: the turn's outcome, from the locked eval.

    This is the record agents read to learn what each commit actually scored; it is written
    only here (agents cannot post the type, see `board.SYSTEM_ONLY_TYPES`), and the ledger
    records its hash so a forged copy is never shown. `status` is "ok", or why the turn has no
    (full) score: "full_eval_failed" (proxy score only), "eval_failed", "timed_out",
    "lock_violation". `best_before` is {"proxy": x, "full": y} from before this commit.
    Its refs are the agent's `result` entry and whatever that entry refs (the proposal it tested).
    """
    maximize = bool(r.m.get("maximize", True))
    final = full_result if full_result is not None else proxy_result
    kind = "full" if full_result is not None else "proxy"
    score = None if final is None else float(final["score"])
    no_change = commit == r.m["baseline_commit"] and agent != "baseline"
    baseline = (r.get("baseline_scores") or {}).get(kind)
    best = (best_before or {}).get(kind)

    refs = []
    if board_entry_id is not None:
        refs.append(board_entry_id)
        for e in board.read(r):
            if e["id"] == board_entry_id:
                refs += [x for x in (e.get("refs") or []) if isinstance(x, int)]
                break

    def delta(ref):
        if score is None or not isinstance(ref, (int, float)):
            return None
        return _sig(score - ref if maximize else ref - score)

    metrics = _metrics(r, final, agent) if final is not None else {}
    vs_best = delta(best)
    vs_base = delta(baseline) if agent != "baseline" else None
    sha = commit[:8] if commit else "no-commit"
    head = f"{agent} r{round} {sha}: "
    head += f"score {score:g} ({kind})" if score is not None else "NOT SCORED"
    bits = [head]
    if status != "ok":
        bits.append(f"{status.upper()}" + (f": {_clip(error, 160)}" if error else ""))
    if no_change:
        bits.append("NO CHANGE (commit is the baseline)")
    if vs_base is not None:
        bits.append(f"vs baseline ({kind}) {vs_base:+g}")
    if vs_best is not None:
        bits.append("NEW BEST" if vs_best > 0 else f"vs best ({kind}) {vs_best:+g}")
    line = _metrics_line(metrics)
    text = "; ".join(bits) + (f" | {line}" if line else "")
    body = {
        "text": text,
        "agent": agent,
        "status": status,
        "commit": commit,
        "score": score,
        "score_kind": kind if score is not None else None,
        "proxy_score": None if proxy_result is None else float(proxy_result["score"]),
        "full_score": None if full_result is None else float(full_result["score"]),
        "promoted": full_result is not None,
        "no_change": no_change,
        "vs_baseline": vs_base,
        "vs_best_before": vs_best,
        "metrics": metrics,
    }
    if error:
        body["error"] = _clip(error, 600)
    return board.append(r, round=round, role="system", author_agent="orchestrator",
                        type="scored", body=body, refs=refs, trusted=True)


def _clip(text, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _sig(x: float) -> float:
    return float(f"{x:.6g}")


def score_result(r, *, worktree, commit, agent, round, turn_id, board_entry_id=None) -> dict:
    """Score a turn's commit with the proxy eval and record it as authoritative.

    The recorded `proxy_score` comes only from `run_eval`'s locked command; any
    score an agent claims on the blackboard is never read here. When the proxy
    score clears `eval.promote_threshold` (or, absent a threshold, beats the best
    proxy_score recorded so far), the full eval also runs and its score is
    recorded with `promoted=1`. Either way one `scored` entry is posted: a failed
    full eval posts the proxy score with status "full_eval_failed" and then raises.
    Returns `{"proxy_score": float, "promoted": bool}`.

    `board_entry_id`, when given, links this results row to the agent's own board
    `result` entry so shared views can surface the authoritative score for that
    entry instead of the agent's self-reported one (see honesty invariant).
    """
    ev = r.m["eval"]
    best_before = {"proxy": best_score_before(r, "proxy"), "full": best_score_before(r, "full")}
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
    full_result = None
    if promote:
        try:
            full_result = run_eval(r, "full_command", worktree, commit, agent, extra={"round": round})
        except CollabError as exc:
            post_outcome_safely(r, agent=agent, round=round, commit=commit, proxy_result=proxy_result,
                                board_entry_id=board_entry_id, best_before=best_before,
                                status="full_eval_failed", error=str(exc))
            raise
        full_score = float(full_result["score"])
        r.db.execute("UPDATE results SET full_score=?, promoted=1 WHERE id=?", (full_score, result_id))
        r.db.commit()
        promoted = True

    post_scored(r, agent=agent, round=round, commit=commit, proxy_result=proxy_result,
                full_result=full_result, board_entry_id=board_entry_id, best_before=best_before)
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
