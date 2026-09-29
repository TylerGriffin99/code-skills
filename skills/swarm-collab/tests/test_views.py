from swarmcollab.model import Run
from swarmcollab import board, views, util


def test_estimate_and_ceiling(run_dir):
    r = Run(run_dir)
    for i in range(200):
        board.append(r, round=1, role="strategist", author_agent="s1",
                     type="result", body={"text": "x" * 300, "score": i})
    view = views.render_view(r, role="strategist", agent="s1", round=1)
    assert views.estimate_tokens(view) <= r.m["view"]["token_ceiling"]


def test_view_never_exceeds_ceiling_after_flood(run_dir):
    r = Run(run_dir)
    for i in range(500):
        board.append(r, round=2, role="implementor", author_agent=f"i{i}",
                     type="result", body={"text": "y" * 500, "score": i})
    for role in ("implementor", "strategist", "critic"):
        v = views.render_view(r, role=role, agent="i0", round=2)
        assert views.estimate_tokens(v) <= r.m["view"]["token_ceiling"]


def test_addressed_message_appears(run_dir):
    r = Run(run_dir)
    board.append(r, round=1, role="strategist", author_agent="s1",
                 type="message", body={"text": "look at plan Z"}, to=["impl-1"])
    v = views.render_view(r, role="implementor", agent="impl-1", round=1)
    assert "plan Z" in v


def _link_score(r, entry, *, proxy, full, promoted):
    r.db.execute("INSERT INTO turns(round,role,agent,model,started_at) VALUES(?,?,?,?,?)",
                 (entry["round"], entry["role"], entry["author_agent"], "m", util.now()))
    tid = r.db.execute("SELECT id FROM turns ORDER BY id DESC LIMIT 1").fetchone()["id"]
    r.db.execute(
        "INSERT INTO results(turn_id,agent,round,proxy_score,full_score,promoted,commit_sha,board_entry_id,at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (tid, entry["author_agent"], entry["round"], proxy, full, promoted, "sha", entry["id"], util.now()),
    )
    r.db.commit()


def test_views_show_and_rank_by_authoritative_score_not_agent_claim(run_dir):
    r = Run(run_dir)
    # Agent A self-reports a wildly inflated 999 but the eval scored it 0.1.
    a = board.append(r, round=1, role="implementor", author_agent="impl-1",
                     type="result", body={"text": "res A", "score": 999})
    _link_score(r, a, proxy=0.1, full=0.1, promoted=1)
    # Agent B self-reports 1 but the eval scored it 5.0 — the real winner.
    b = board.append(r, round=1, role="implementor", author_agent="impl-2",
                     type="result", body={"text": "res B", "score": 1})
    _link_score(r, b, proxy=5.0, full=5.0, promoted=1)

    v = views.render_view(r, role="strategist", agent="s1", round=1)
    # The authoritative numbers are shown; the agent's self-report is never rendered.
    assert f"#{a['id']} result 0.1" in v
    assert f"#{b['id']} result 5" in v
    assert "999" not in v
    # And ranking is by the authoritative score: B (5.0) outranks A (0.1).
    assert v.index("res B") < v.index("res A")


def test_compaction_reduces_live_region(run_dir):
    r = Run(run_dir)
    for i in range(60):  # threshold is 50
        board.append(r, round=1, role="strategist", author_agent="s1",
                     type="hypothesis", body={"text": f"h{i}"})
    assert views.needs_compaction(r) is True
    digest = views.compact(r, summarizer=lambda entries: f"digest of {len(entries)} entries")
    assert digest["type"] == "digest"
    assert views.needs_compaction(r) is False
