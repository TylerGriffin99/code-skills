import json
import os
import sys
from pathlib import Path

from swarmcollab.model import Run
from swarmcollab import agents, util

MOCK = str(Path(__file__).parent / "mock_agent.py")


def _insert_turn(r, role="implementor", agent="implementor-1", round=1):
    r.db.execute(
        "INSERT INTO turns(round, role, agent, model, started_at) VALUES(?,?,?,?,?)",
        (round, role, agent, "m", util.now()),
    )
    r.db.commit()
    return r.db.execute("SELECT id FROM turns ORDER BY id DESC LIMIT 1").fetchone()["id"]


def test_reap_turn_records_cost_tokens_and_ended_at(run_dir):
    r = Run(run_dir)
    turn_id = _insert_turn(r)
    logs = run_dir / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "implementor-implementor-1-r1.json").write_text(
        json.dumps({"total_cost_usd": 0.42, "num_turns": 3, "usage": {"output_tokens": 1234}})
    )
    agents.reap_turn(r, role="implementor", agent="implementor-1", round=1, turn_id=turn_id, returncode=0)
    row = r.db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
    assert row["cost_usd"] == 0.42
    assert row["tokens"] == 1234
    assert row["ended_at"] is not None
    assert row["exit_code"] == 0
    assert row["is_error"] is None


def test_reap_turn_nonzero_exit_is_distinct_from_drop_missing(run_dir):
    r = Run(run_dir)
    turn_id = _insert_turn(r)
    # No usage log written: the crash must still be recorded distinctly.
    agents.reap_turn(r, role="implementor", agent="implementor-1", round=1, turn_id=turn_id, returncode=2)
    row = r.db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
    assert row["exit_code"] == 2 and row["is_error"] == 1
    kinds = [e["kind"] for e in r.db.execute("SELECT kind FROM events").fetchall()]
    assert "turn_process_failed" in kinds          # a crashed agent is loudly recorded
    assert "drop_missing" not in kinds             # and never folded into drop_missing
    # A genuinely-absent drop is a *separate*, distinguishable signal:
    agents.ingest_drop(r, role="implementor", agent="implementor-1", round=1, drop_file=run_dir / "nope.jsonl")
    kinds2 = [e["kind"] for e in r.db.execute("SELECT kind FROM events").fetchall()]
    assert "drop_missing" in kinds2


def test_role_prompt_carries_drop_file_contract(run_dir, tmp_repo):
    """The rendered real prompt must teach the $COLLAB_DROP drop-file contract from
    roles/{role}.md, with the {agent}/{round}/{goal}/{view} placeholders filled in."""
    r = Run(run_dir)
    prompt = agents._build_prompt(r, role="implementor", agent="implementor-1", round=2)
    assert "COLLAB_DROP" in prompt
    assert "drop-file contract" in prompt
    # placeholders were rendered, not left literal
    for placeholder in ("{view}", "{agent}", "{round}", "{goal}"):
        assert placeholder not in prompt
    assert "implementor-1" in prompt        # {agent}
    assert r.m["goal"] in prompt            # {goal}


def test_ingest_skips_malformed_lines_and_processes_the_rest(run_dir):
    r = Run(run_dir)
    drop = run_dir / "d.jsonl"
    drop.write_text("\n".join([
        "{ not valid json",                                       # undecodable
        json.dumps({"body": {"text": "no type field"}}),          # missing 'type'
        json.dumps({"type": "banana", "body": {"text": "x"}}),    # unknown type -> board rejects
        json.dumps({"type": "result", "body": {"text": "good one"}}),  # valid, must survive
    ]))
    accepted = agents.ingest_drop(r, role="implementor", agent="i1", round=1, drop_file=drop)
    assert [e["body"]["text"] for e in accepted] == ["good one"]
    mal = r.db.execute("SELECT COUNT(*) c FROM events WHERE kind='drop_line_malformed'").fetchone()["c"]
    assert mal == 3


def test_ingest_enforces_message_quota(run_dir):
    r = Run(run_dir)  # per_turn_quota == 2, max_tokens == 200
    drop = run_dir / "drop.jsonl"
    lines = [
        {"type": "message", "body": {"text": "m1"}, "to": ["impl-2"]},
        {"type": "message", "body": {"text": "m2"}, "to": ["impl-2"]},
        {"type": "message", "body": {"text": "m3 over quota"}, "to": ["impl-2"]},
        {"type": "message", "body": {"text": "x" * 2000}, "to": ["impl-2"]},  # oversized
        {"type": "proposal", "body": {"text": "not a message, always allowed"}},
    ]
    drop.write_text("\n".join(json.dumps(x) for x in lines))
    accepted = agents.ingest_drop(r, role="strategist", agent="s1", round=1, drop_file=drop)
    msgs = [e for e in accepted if e["type"] == "message"]
    assert len(msgs) == 2  # third dropped for quota, fourth for size
    assert any(e["type"] == "proposal" for e in accepted)


def test_message_over_quota_dropped(run_dir):
    r = Run(run_dir)
    drop = run_dir / "d.jsonl"
    drop.write_text("\n".join(json.dumps(
        {"type": "message", "body": {"text": f"m{i}"}, "to": ["a"]}) for i in range(5)))
    agents.ingest_drop(r, role="s", agent="s1", round=1, drop_file=drop)
    events = r.db.execute("SELECT COUNT(*) c FROM events WHERE kind='message_dropped'").fetchone()["c"]
    assert events == 3  # 5 sent, quota 2, so 3 dropped


def test_launch_turn_with_mock_agent(run_dir, tmp_repo):
    r = Run(run_dir)
    script = run_dir / "script.jsonl"
    script.write_text(json.dumps({"type": "hypothesis", "body": {"text": "mocked idea"}}))
    os.environ["COLLAB_AGENT_CMD"] = f"{sys.executable} {MOCK}"
    os.environ["COLLAB_MOCK_SCRIPT"] = str(script)
    try:
        drop_dir = run_dir / "drops"
        drop_dir.mkdir()
        proc = agents.launch_turn(r, role="strategist", agent="s1", model="m",
                                  round=1, cwd=tmp_repo, drop_dir=drop_dir)
        assert proc.wait(timeout=30) == 0
        drop = drop_dir / "strategist-s1-r1.jsonl"
        accepted = agents.ingest_drop(r, role="strategist", agent="s1", round=1, drop_file=drop)
        assert accepted[0]["body"]["text"] == "mocked idea"
    finally:
        del os.environ["COLLAB_AGENT_CMD"], os.environ["COLLAB_MOCK_SCRIPT"]
