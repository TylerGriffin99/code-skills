"""Budget accounting: global, per-round, and per-agent caps against the turns ledger."""

from __future__ import annotations

import datetime as dt

from swarmcollab.util import now


def totals(r) -> dict:
    row = r.db.execute(
        "SELECT COALESCE(SUM(cost_usd),0) AS cost, COALESCE(SUM(tokens),0) AS tokens,"
        " COUNT(*) AS turns, SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END) AS running"
        " FROM turns"
    ).fetchone()
    return {
        "cost": row["cost"],
        "tokens": row["tokens"],
        "turns": row["turns"],
        "running": row["running"] or 0,
    }


def round_tokens(r, round) -> int:
    row = r.db.execute(
        "SELECT COALESCE(SUM(tokens),0) AS tokens FROM turns WHERE round=?", (round,)
    ).fetchone()
    return row["tokens"]


def budget_reason(r, round_no: int) -> str | None:
    b = r.budget()
    t = totals(r)
    if t["cost"] >= b["max_usd"]:
        return f"budget: ${t['cost']:.2f} spent of ${b['max_usd']}"
    if round_no >= b["max_rounds"]:
        return f"budget: round {round_no} of {b['max_rounds']}"
    started = dt.datetime.fromisoformat(r.get("run_started_at", now()))
    hours = (dt.datetime.now() - started).total_seconds() / 3600
    if hours >= b["max_hours"]:
        return f"budget: {hours:.1f}h of {b['max_hours']}h"
    return None


def agent_cap_exceeded(r, agent, round) -> bool:
    b = r.budget()
    row = r.db.execute(
        "SELECT COALESCE(SUM(tokens),0) AS tokens FROM turns WHERE agent=? AND round=?",
        (agent, round),
    ).fetchone()
    return row["tokens"] >= b["agent_token_cap"]


def round_cap_reason(r, round) -> str | None:
    b = r.budget()
    tokens = round_tokens(r, round)
    if tokens >= b["round_token_cap"]:
        return f"round budget: {tokens} tokens of {b['round_token_cap']} in round {round}"
    return None
