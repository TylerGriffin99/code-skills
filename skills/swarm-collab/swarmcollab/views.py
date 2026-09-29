"""Per-agent board views: a token-capped renderer and deterministic compaction.

`render_view` GUARANTEES the returned text estimates at or under
`view.token_ceiling`, no matter how many entries are on the board, by
trimming lowest-priority entries until it fits. `compact` keeps the live
region small by folding the oldest entries into a single `digest` entry.
"""

import json
from math import ceil

from swarmcollab import board
from swarmcollab.util import CollabError

ONE_LINE_MAX_CHARS = 140
GOAL_MAX_CHARS = 300
LOCKED_MAX_CHARS = 200
DIGEST_MAX_CHARS = 1500


def estimate_tokens(text: str) -> int:
    return max(1, ceil(len(text) / 4))


def _truncate(text: str, n: int) -> str:
    text = str(text)
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)] + "…"


def _clip_line(text, n: int) -> str:
    text = " ".join(str(text).split())
    return _truncate(text, n)


def _current_best(r):
    maximize = bool(r.m.get("maximize", True))
    agg = "MAX" if maximize else "MIN"
    row = r.db.execute(
        f"SELECT {agg}(full_score) AS best FROM results WHERE full_score IS NOT NULL"
    ).fetchone()
    return row["best"] if row is not None else None


def pinned_facts(r) -> str:
    """Compact, fixed-shape facts that always fit well inside the ceiling."""
    goal = _clip_line(r.m.get("goal", ""), GOAL_MAX_CHARS)
    maximize = bool(r.m.get("maximize", True))
    best = _current_best(r)
    best_str = f"{best:g}" if isinstance(best, (int, float)) else "n/a"
    locked = r.m.get("locked_files") or []
    locked_str = _clip_line(", ".join(locked), LOCKED_MAX_CHARS) if locked else "none"
    entries = board.read(r)
    round_no = entries[-1]["round"] if entries else 0
    return "\n".join([
        f"Goal: {goal}",
        f"Best score ({'max' if maximize else 'min'}): {best_str}",
        f"Locked files: {locked_str}",
        f"Round: {round_no}",
    ])


def _authoritative_scores(r) -> dict:
    """Map board `result`-entry id -> the swarm's own authoritative score for it.

    The score is the eval's `full_score` when the result was promoted, else its
    `proxy_score`, read from the results ledger via `results.board_entry_id`. An
    agent's self-reported `body["score"]` is never used to rank or display a
    result — only the locked eval's number is (the honesty invariant).
    """
    out = {}
    for row in r.db.execute(
        "SELECT board_entry_id, proxy_score, full_score FROM results WHERE board_entry_id IS NOT NULL"
    ):
        score = row["full_score"] if row["full_score"] is not None else row["proxy_score"]
        if score is None:
            continue
        bid = row["board_entry_id"]
        # If several results rows ever point at one entry, the promoted/full one wins.
        if bid not in out or row["full_score"] is not None:
            out[bid] = score
    return out


def _score(e) -> float:
    """The authoritative score annotated onto a result entry, or -inf if none.

    Deliberately reads only `_auth_score` (stamped from the eval ledger in
    `render_view`), never `body["score"]`: an agent's self-reported number must
    never influence ranking.
    """
    score = e.get("_auth_score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return score
    return float("-inf")


def _one_line_body(body, max_chars: int = ONE_LINE_MAX_CHARS) -> str:
    if isinstance(body, dict) and "text" in body:
        text = body["text"]
    else:
        text = json.dumps(body, sort_keys=True) if body else ""
    return _clip_line(text, max_chars)


def _render_line(e) -> str:
    body = e.get("body") or {}
    # Show only the authoritative eval score (annotated onto the entry), never the
    # agent's self-reported body["score"]; unscored entries render "-".
    score = e.get("_auth_score")
    score_str = f"{score:g}" if isinstance(score, (int, float)) and not isinstance(score, bool) else "-"
    return f"[#{e['id']} {e['type']} {score_str}] {_one_line_body(body)}"


def _dedupe(entries) -> list:
    seen = set()
    out = []
    for e in entries:
        if e["id"] not in seen:
            seen.add(e["id"])
            out.append(e)
    return out


def _by_id_desc(entries) -> list:
    return sorted(entries, key=lambda e: e["id"], reverse=True)


def _by_score_desc(entries) -> list:
    return sorted(entries, key=lambda e: (_score(e), e["id"]), reverse=True)


def _messages_to(entries, agent) -> list:
    return _by_id_desc([e for e in entries if e["type"] == "message" and agent in (e.get("to") or [])])


def _implementor_candidates(entries, agent) -> list:
    proposals = [e for e in entries if e["type"] == "proposal"]
    own = [e for e in proposals if e["author_agent"] == agent]
    assigned = _by_id_desc(own)[:1] or _by_id_desc(proposals)[:1]
    assigned_ids = {e["id"] for e in assigned}
    critiques = _by_id_desc(
        [e for e in entries if e["type"] == "critique" and (set(e.get("refs") or []) & assigned_ids)]
    )
    deadends = _by_id_desc([e for e in entries if e["type"] == "deadend"])
    results = _by_score_desc([e for e in entries if e["type"] == "result"])
    messages = _messages_to(entries, agent)
    return _dedupe(assigned + critiques + deadends + results + messages)


def _strategist_candidates(entries, agent) -> list:
    latest_digest = _by_id_desc([e for e in entries if e["type"] == "digest"])[:1]
    results = _by_score_desc([e for e in entries if e["type"] == "result"])
    critiques = _by_id_desc([e for e in entries if e["type"] == "critique"])
    proposals = _by_id_desc([e for e in entries if e["type"] == "proposal"])
    messages = _messages_to(entries, agent)
    return _dedupe(latest_digest + results + critiques + proposals + messages)


def _critic_candidates(entries, agent) -> list:
    critiqued_refs = set()
    for e in entries:
        if e["type"] == "critique":
            critiqued_refs.update(e.get("refs") or [])
    uncritiqued = _by_id_desc(
        [e for e in entries if e["type"] in ("proposal", "result") and e["id"] not in critiqued_refs]
    )
    messages = _messages_to(entries, agent)
    return _dedupe(uncritiqued + messages)


ROLE_BUILDERS = {
    "implementor": _implementor_candidates,
    "strategist": _strategist_candidates,
    "critic": _critic_candidates,
}


def _archived_max_id(r) -> int:
    return r.get("archived_max_id", 0)


def _live_entries(r) -> list:
    """All board entries still relevant to a view: never-archived plus every digest."""
    archived_max_id = _archived_max_id(r)
    return [e for e in board.read(r) if e["type"] == "digest" or e["id"] > archived_max_id]


def render_view(r, *, role: str, agent: str, round: int) -> str:
    builder = ROLE_BUILDERS.get(role)
    if builder is None:
        raise CollabError(f"unknown role for view rendering: {role!r}")

    pinned = pinned_facts(r)
    entries = _live_entries(r)
    # Stamp each result entry with the swarm's authoritative score so both ranking
    # (_by_score_desc) and display (_render_line) use the eval's number, not the
    # agent's self-report.
    auth = _authoritative_scores(r)
    for e in entries:
        if e["type"] == "result":
            e["_auth_score"] = auth.get(e["id"])
    candidates = builder(entries, agent)
    lines = [_render_line(e) for e in candidates]
    ceiling = r.m["view"]["token_ceiling"]

    def assemble(kept):
        return pinned if not kept else pinned + "\n\n" + "\n".join(kept)

    view = assemble(lines)
    while lines and estimate_tokens(view) > ceiling:
        lines.pop()  # drop the lowest-priority (tail) entry first
        view = assemble(lines)
    return view


def needs_compaction(r) -> bool:
    archived_max_id = _archived_max_id(r)
    threshold = r.m["view"]["compact_threshold_entries"]
    live_count = sum(1 for e in board.read(r) if e["type"] != "digest" and e["id"] > archived_max_id)
    return live_count >= threshold


def default_summarizer(entries) -> str:
    """Deterministic v1 reducer: one line per entry, no LLM involved."""
    return "\n".join(_render_line(e) for e in entries)


def compact(r, *, summarizer=None) -> dict:
    if summarizer is None:
        summarizer = default_summarizer

    archived_max_id = _archived_max_id(r)
    threshold = r.m["view"]["compact_threshold_entries"]
    live = [e for e in board.read(r) if e["type"] != "digest" and e["id"] > archived_max_id]
    to_archive = live[:threshold]
    if not to_archive:
        raise CollabError("compact() called with nothing eligible to archive")

    text = _truncate(summarizer(to_archive), DIGEST_MAX_CHARS)
    new_archived_max_id = max(e["id"] for e in to_archive)
    digest = board.append(
        r, round=to_archive[-1]["round"], role="system", author_agent="compactor",
        type="digest", body={"text": text}, refs=[e["id"] for e in to_archive],
    )
    r.set("archived_max_id", new_archived_max_id)
    return digest
