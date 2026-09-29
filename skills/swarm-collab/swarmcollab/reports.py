"""Markdown summaries of a swarm-collab run: `pilot_report` and `harvest`.

Both read only from the locked ledger (the `turns`/`results` tables and the board) —
never from an agent's own self-reported claim — so every number here traces back to
`evaluate.run_eval`'s authoritative score or the orchestrator's own bookkeeping.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from swarmcollab import board, evaluate, util


def _best_result_row(r):
    maximize = bool(r.m.get("maximize", True))
    order = "DESC" if maximize else "ASC"
    return r.db.execute(
        f"SELECT * FROM results WHERE full_score IS NOT NULL ORDER BY full_score {order} LIMIT 1"
    ).fetchone()


def _rounds_run(r) -> int:
    row = r.db.execute("SELECT COALESCE(MAX(round), 0) AS m FROM turns").fetchone()
    return row["m"] if row is not None else 0


def _totals(r):
    return r.db.execute(
        "SELECT COUNT(*) AS turns, COALESCE(SUM(tokens),0) AS tokens,"
        " COALESCE(SUM(cost_usd),0) AS cost FROM turns"
    ).fetchone()


def _tokens_by_role(r):
    return r.db.execute(
        "SELECT role, COUNT(*) AS turns, COALESCE(SUM(tokens),0) AS tokens,"
        " COALESCE(SUM(cost_usd),0) AS cost FROM turns GROUP BY role ORDER BY role"
    ).fetchall()


def _score_by_round_agent(r) -> dict:
    """Map (round, agent) -> the best authoritative score recorded for that turn
    (full_score when promoted, else proxy_score). Used to link a board `result`
    entry back to the score the swarm itself computed for it.

    "Best" respects the manifest's `maximize` flag, same as `_best_result_row`: for a
    minimize-mode swarm the lowest score per (round, agent) is kept, not the highest.
    """
    maximize = bool(r.m.get("maximize", True))
    out = {}
    for row in r.db.execute("SELECT round, agent, proxy_score, full_score FROM results"):
        score = row["full_score"] if row["full_score"] is not None else row["proxy_score"]
        if score is None:
            continue
        key = (row["round"], row["agent"])
        prev = out.get(key)
        if prev is None or (score > prev if maximize else score < prev):
            out[key] = score
    return out


def _critiqued_proposal_ids(r) -> set:
    ids = set()
    for entry in board.read(r, types=["critique"]):
        ids.update(entry.get("refs") or [])
    return ids


def collaboration_roi(r) -> dict:
    """Compare the scores of `result` entries that reference (via `refs`) a critiqued
    proposal against those that don't — the spec's "does collaboration add value" check.

    Never fabricates a verdict from too little data: if either side has no scored
    entries (no critiques were posted, or no result referenced a proposal), the
    returned dict carries an explanatory `note` instead of a comparison.
    """
    score_by_key = _score_by_round_agent(r)
    critiqued_ids = _critiqued_proposal_ids(r)
    critiqued_scores, uncritiqued_scores = [], []
    for entry in board.read(r, types=["result"]):
        key = (entry["round"], entry["author_agent"])
        score = score_by_key.get(key)
        if score is None:
            continue
        refs = set(entry.get("refs") or [])
        if refs & critiqued_ids:
            critiqued_scores.append(score)
        else:
            uncritiqued_scores.append(score)

    out = {"critiqued_scores": critiqued_scores, "uncritiqued_scores": uncritiqued_scores}
    if not critiqued_scores or not uncritiqued_scores:
        out["note"] = (
            "insufficient data: need at least one scored result linked (via refs) to a"
            " critiqued proposal, and one that isn't, to compare — got "
            f"{len(critiqued_scores)} critiqued and {len(uncritiqued_scores)} un-critiqued"
        )
        return out

    avg_c = sum(critiqued_scores) / len(critiqued_scores)
    avg_u = sum(uncritiqued_scores) / len(uncritiqued_scores)
    out["avg_critiqued"] = avg_c
    out["avg_uncritiqued"] = avg_u
    if avg_c > avg_u:
        verdict = "critique added value"
    elif avg_c < avg_u:
        verdict = "no advantage seen from critique"
    else:
        verdict = "no difference seen"
    out["note"] = (
        f"critiqued proposals averaged {avg_c:g} (n={len(critiqued_scores)}) vs"
        f" {avg_u:g} (n={len(uncritiqued_scores)}) for un-critiqued: {verdict}"
    )
    return out


def _verify_one(r, commit: str, recorded: float):
    """Re-run the full eval on `commit` in an ephemeral detached worktree.

    Returns `(ok, new_score)` from `evaluate.verify`. The worktree is always
    removed afterward; harvest never merges a lane or runs a lockbox command.
    """
    tmp = Path(tempfile.mkdtemp(prefix="collab-verify-"))
    wt = tmp / "wt"
    try:
        util.git(r.repo, "worktree", "add", "-q", "--detach", str(wt), commit)
        return evaluate.verify(r, worktree=wt, commit=commit, recorded=recorded)
    finally:
        util.run(["git", "-C", str(r.repo), "worktree", "remove", "--force", str(wt)], check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def _verify_lines(r) -> list:
    """Re-verify every promoted result (re-run full eval, compare to recorded score).

    A verify failure for any one result is reported, never allowed to crash harvest:
    an eval that errors out shows as an ERROR row, and any unexpected failure of the
    whole step degrades to a single explanatory line.
    """
    try:
        rows = r.db.execute(
            "SELECT agent, round, commit_sha, full_score FROM results"
            " WHERE promoted=1 AND full_score IS NOT NULL ORDER BY id"
        ).fetchall()
    except Exception as exc:  # pragma: no cover - defensive
        return [f"_Verification skipped: {type(exc).__name__}: {exc}_"]

    if not rows:
        return ["No promoted results to verify."]

    out = ["| Agent | Round | Recorded | Re-run | Δ | Verify |", "|---|---|---|---|---|---|"]
    for row in rows:
        recorded = row["full_score"]
        try:
            ok, new_score = _verify_one(r, row["commit_sha"], recorded)
            verdict = "pass" if ok else "FAIL"
            delta = new_score - recorded
            out.append(
                f"| {row['agent']} | {row['round']} | {recorded:g} | {new_score:g} | {delta:+g} | {verdict} |"
            )
        except Exception as exc:
            out.append(f"| {row['agent']} | {row['round']} | {recorded:g} | — | — | ERROR: {str(exc)[:80]} |")
    return out


def _summary_lines(r) -> list:
    maximize = bool(r.m.get("maximize", True))
    best_row = _best_result_row(r)
    target = r.m.get("target_score")
    totals = _totals(r)

    best_line = "Best full_score: n/a"
    commit_line = "Best commit: n/a"
    if best_row is not None:
        best_line = f"Best full_score ({'max' if maximize else 'min'}): {best_row['full_score']:g}"
        if isinstance(target, (int, float)):
            best_line += f"  (target {target:g})"
        commit_line = f"Best commit: {best_row['commit_sha']}  (agent {best_row['agent']}, round {best_row['round']})"

    lines = [
        f"Rounds run: {_rounds_run(r)}",
        best_line,
        commit_line,
        f"Turns: {totals['turns']}  tokens: {totals['tokens']}  cost: ${totals['cost']:.2f}",
        "",
        "### Per-role spend",
        "",
        "| Role | Turns | Tokens | Cost |",
        "|---|---|---|---|",
    ]
    role_rows = _tokens_by_role(r)
    if not role_rows:
        lines.append("| (none) | 0 | 0 | $0.00 |")
    for row in role_rows:
        lines.append(f"| {row['role']} | {row['turns']} | {row['tokens']} | ${row['cost']:.2f} |")
    if role_rows and totals["turns"] and not totals["tokens"] and not totals["cost"]:
        lines.append(
            "\n_Note: every turn shows 0 tokens / $0.00 — this run's agent process never reported"
            " usage into the ledger; treat this as untracked, not as a genuinely free run._"
        )

    roi = collaboration_roi(r)
    lines += ["", "### Collaboration ROI (does critique add value?)", "", roi["note"]]
    return lines


def pilot_report(r) -> str:
    lines = [f"# Pilot report: {r.run_id}", "", f"Goal: {r.m.get('goal', '')}", ""]
    lines += _summary_lines(r)
    lines += ["", "Decide: continue toward the full run, or stop here (`scale` / `stop`)."]
    return "\n".join(lines) + "\n"


def harvest(r) -> str:
    lines = [f"# Harvest: {r.run_id}", "", f"Goal: {r.m.get('goal', '')}", ""]
    lines += _summary_lines(r)
    lines += [
        "",
        "### Verification (full eval re-run vs recorded score)",
        "",
    ]
    lines += _verify_lines(r)
    lines += [
        "",
        "### Fixed ground",
        "",
        "This report only summarizes scores the locked eval itself computed; the orchestrator"
        " never merged a lane and never ran a lockbox command. A human must review and land"
        " any winning commit named above.",
    ]
    return "\n".join(lines) + "\n"
