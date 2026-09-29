import json
from pathlib import Path

import pytest

from swarmcollab.model import Run
from swarmcollab.util import CollabError
from swarmcollab import scheduler, board, locks, util


def fake_launch_factory(scripts):
    """Return a launch(r, role, agent, round, cwd, drop_dir, model=...) that writes scripted drops
    synchronously and returns a dummy finished process-like object."""
    class Done:
        returncode = 0
        def wait(self, timeout=None): return 0
        def poll(self): return 0
    def launch(r, *, role, agent, model, round, cwd, drop_dir):
        drop = Path(drop_dir) / f"{role}-{agent}-r{round}.jsonl"
        drop.write_text("\n".join(json.dumps(x) for x in scripts.get(role, [])))
        r.db.execute("INSERT INTO turns(round, role, agent, model, started_at, ended_at, cost_usd, tokens)"
                     " VALUES(?,?,?,?,?,?,?,?)", (round, role, agent, model, util.now(), util.now(), 0.01, 100))
        r.db.commit()
        return Done()
    return launch


def test_run_round_posts_entries_from_each_role(run_dir, tmp_repo, monkeypatch):
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    r = Run(run_dir); r.set("run_started_at", util.now()); locks.write_locks(r); locks.advance_board_prefix(r)
    scripts = {
        "strategist": [{"type": "proposal", "body": {"text": "try adding one"}}],
        "critic": [{"type": "critique", "body": {"text": "cheap"}}],
        "implementor": [{"type": "result", "body": {"text": "did it"}}],
    }
    # make eval trivial and cheap
    m = json.loads((run_dir / "swarm.json").read_text())
    m["eval"] = {"proxy_command": "python3 -c 'print(\"{\\\"score\\\":1}\")'",
                 "full_command": "python3 -c 'print(\"{\\\"score\\\":1}\")'", "verify_tolerance": 0}
    (run_dir / "swarm.json").write_text(json.dumps(m)); r = Run(run_dir)
    monkeypatch.setattr(scheduler, "make_worktree", lambda r, agent, c: tmp_repo)
    monkeypatch.setattr(scheduler, "reap_worktree", lambda r, p: None)
    scheduler.run_round(r, 1, launch=fake_launch_factory(scripts))
    assert board.read(r, types=["proposal"]) and board.read(r, types=["critique"])
    assert board.read(r, types=["result"])


def test_termination_on_control_lock(run_dir, tmp_repo):
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    r = Run(run_dir); r.set("run_started_at", util.now()); locks.write_locks(r)
    (run_dir / "swarm.json").write_text((run_dir / "swarm.json").read_text() + "\n# tamper\n")
    assert scheduler.termination_reason(r, 1).startswith("HALT")


def test_termination_on_target_score(run_dir, tmp_repo):
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    m = json.loads((run_dir / "swarm.json").read_text()); m["target_score"] = 0.9
    (run_dir / "swarm.json").write_text(json.dumps(m))
    r = Run(run_dir); r.set("run_started_at", util.now()); locks.write_locks(r); locks.advance_board_prefix(r)
    r.db.execute("INSERT INTO turns(round,role,agent,model,started_at) VALUES(1,'implementor','i1','m',?)",
                 (util.now(),))
    r.db.execute("INSERT INTO results(turn_id,agent,round,full_score,at) VALUES(1,'i1',1,0.95,?)",
                 (util.now(),)); r.db.commit()
    assert "target" in scheduler.termination_reason(r, 1).lower()


def test_implementor_worktree_reaped_and_round_continues_when_eval_raises(run_dir, tmp_repo, monkeypatch):
    """A CollabError from evaluate.score_result for one implementor must not leak its
    worktree, must not abort the other implementor, and must be recorded (not swallowed)."""
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    r = Run(run_dir); r.set("run_started_at", util.now()); locks.write_locks(r); locks.advance_board_prefix(r)
    scripts = {
        "strategist": [{"type": "proposal", "body": {"text": "try adding one"}}],
        "critic": [{"type": "critique", "body": {"text": "cheap"}}],
        "implementor": [{"type": "result", "body": {"text": "did it"}}],
    }

    reaped = []
    monkeypatch.setattr(scheduler, "make_worktree", lambda r, agent, c: tmp_repo)
    monkeypatch.setattr(scheduler, "reap_worktree", lambda r, p: reaped.append(p))

    def failing_score_result(r, **kwargs):
        raise CollabError("eval command exploded")
    monkeypatch.setattr(scheduler.evaluate, "score_result", failing_score_result)

    # roles.implementor == 2 in base_manifest: both must be attempted despite the failure.
    scheduler.run_round(r, 1, launch=fake_launch_factory(scripts))

    # both implementors' worktrees were reaped despite the eval failure
    assert len(reaped) == 2
    # both implementors' drops were still ingested (result entries posted) before scoring failed
    assert len(board.read(r, types=["result"])) == 2
    # both turns were recorded as errored, not silently dropped
    rows = r.db.execute("SELECT is_error, note FROM turns WHERE role='implementor'").fetchall()
    assert len(rows) == 2
    assert all(row["is_error"] == 1 for row in rows)
    assert all(row["note"] and "eval command exploded" in row["note"] for row in rows)
    # the failure was logged as an event, not swallowed
    events = r.db.execute("SELECT COUNT(*) c FROM events WHERE kind='turn_failed'").fetchone()["c"]
    assert events == 2


def _counting_launch(launches, tokens=1):
    """A launch that records each agent it starts and inserts a turn with `tokens`."""
    class Done:
        returncode = 0
        def wait(self, timeout=None): return 0
        def poll(self): return 0

    def launch(r, *, role, agent, model, round, cwd, drop_dir):
        launches.append(agent)
        drop = Path(drop_dir) / f"{role}-{agent}-r{round}.jsonl"
        drop.write_text(json.dumps({"type": "proposal", "body": {"text": "p"}}))
        r.db.execute("INSERT INTO turns(round,role,agent,model,started_at,tokens) VALUES(?,?,?,?,?,?)",
                     (round, role, agent, model, util.now(), tokens))
        r.db.commit()
        return Done()
    return launch


def test_round_cap_stops_further_launches(run_dir):
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    m = json.loads((run_dir / "swarm.json").read_text())
    m["roles"] = {"strategist": 3, "implementor": 0, "critic": 0}
    m["budget"]["round_token_cap"] = 100000
    (run_dir / "swarm.json").write_text(json.dumps(m))
    r = Run(run_dir); r.set("run_started_at", util.now()); locks.write_locks(r); locks.advance_board_prefix(r)

    launches = []
    drop_dir = run_dir / "drops"; drop_dir.mkdir()
    # each turn burns the whole round cap, so only the first should ever launch
    scheduler._run_role_turns(r, "strategist", 1, _counting_launch(launches, tokens=100000), drop_dir)
    assert launches == ["strategist-1"]
    assert r.db.execute("SELECT COUNT(*) c FROM events WHERE kind='round_cap_reached'").fetchone()["c"] >= 1


def test_agent_cap_skips_the_over_cap_agent(run_dir):
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    m = json.loads((run_dir / "swarm.json").read_text())
    m["roles"] = {"strategist": 2, "implementor": 0, "critic": 0}
    m["budget"]["round_token_cap"] = 10**9
    m["budget"]["agent_token_cap"] = 100
    (run_dir / "swarm.json").write_text(json.dumps(m))
    r = Run(run_dir); r.set("run_started_at", util.now()); locks.write_locks(r); locks.advance_board_prefix(r)
    # strategist-1 is already over its per-agent cap for round 1
    r.db.execute("INSERT INTO turns(round,role,agent,model,started_at,tokens) VALUES(1,'strategist','strategist-1','m',?,?)",
                 (util.now(), 500)); r.db.commit()

    launches = []
    drop_dir = run_dir / "drops"; drop_dir.mkdir()
    scheduler._run_role_turns(r, "strategist", 1, _counting_launch(launches, tokens=1), drop_dir)
    assert launches == ["strategist-2"]  # 1 skipped, 2 still runs
    assert r.db.execute("SELECT COUNT(*) c FROM events WHERE kind='agent_cap_reached'").fetchone()["c"] == 1


def test_malformed_implementor_drop_still_reaps_and_processes_others(run_dir, tmp_repo, monkeypatch):
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    m = json.loads((run_dir / "swarm.json").read_text())
    m["roles"] = {"strategist": 0, "implementor": 2, "critic": 0}
    m["rounds"] = {"strategist_every": 1, "critic_every": 1, "council_every": 99}
    m["eval"] = {"proxy_command": "python3 -c 'print(\"{\\\"score\\\":1}\")'",
                 "full_command": "python3 -c 'print(\"{\\\"score\\\":1}\")'", "verify_tolerance": 0}
    (run_dir / "swarm.json").write_text(json.dumps(m))
    r = Run(run_dir); r.set("run_started_at", util.now()); locks.write_locks(r); locks.advance_board_prefix(r)

    reaped = []
    monkeypatch.setattr(scheduler, "make_worktree", lambda r, agent, c: tmp_repo)
    monkeypatch.setattr(scheduler, "reap_worktree", lambda r, p: reaped.append(p))

    class Done:
        returncode = 0
        def wait(self, timeout=None): return 0
        def poll(self): return 0

    def launch(r, *, role, agent, model, round, cwd, drop_dir):
        drop = Path(drop_dir) / f"{role}-{agent}-r{round}.jsonl"
        if agent == "implementor-1":  # a truncated line THEN a good one
            drop.write_text("{ bad json\n" + json.dumps({"type": "result", "body": {"text": "i1 ok"}}))
        else:
            drop.write_text(json.dumps({"type": "result", "body": {"text": "i2 ok"}}))
        r.db.execute("INSERT INTO turns(round,role,agent,model,started_at) VALUES(?,?,?,?,?)",
                     (round, role, agent, model, util.now())); r.db.commit()
        return Done()

    scheduler.run_round(r, 1, launch=launch)
    assert len(reaped) == 2  # both worktrees reaped despite i1's malformed line
    assert sorted(e["body"]["text"] for e in board.read(r, types=["result"])) == ["i1 ok", "i2 ok"]
    assert r.db.execute("SELECT COUNT(*) c FROM events WHERE kind='drop_line_malformed'").fetchone()["c"] == 1


def test_target_reached_via_proxy_when_no_full_score(run_dir, tmp_repo):
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    m = json.loads((run_dir / "swarm.json").read_text()); m["target_score"] = 0.9
    (run_dir / "swarm.json").write_text(json.dumps(m))
    r = Run(run_dir); r.set("run_started_at", util.now()); locks.write_locks(r); locks.advance_board_prefix(r)
    r.db.execute("INSERT INTO turns(round,role,agent,model,started_at) VALUES(1,'implementor','i1','m',?)", (util.now(),))
    # only a proxy_score exists — nothing promoted to a full_score yet
    r.db.execute("INSERT INTO results(turn_id,agent,round,proxy_score,at) VALUES(1,'i1',1,0.95,?)", (util.now(),))
    r.db.commit()
    reason = scheduler.termination_reason(r, 1)
    assert reason is not None and "target" in reason.lower() and "proxy" in reason.lower()


def test_stall_via_proxy_when_no_full_score(run_dir, tmp_repo):
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    m = json.loads((run_dir / "swarm.json").read_text())
    m["budget"]["max_rounds"] = 99  # keep the round-budget stop from pre-empting the stall
    m["rounds"] = {"strategist_every": 1, "critic_every": 1, "council_every": 1, "stall_rounds": 2}
    (run_dir / "swarm.json").write_text(json.dumps(m))
    r = Run(run_dir); r.set("run_started_at", util.now()); locks.write_locks(r); locks.advance_board_prefix(r)
    for rnd in (1, 2, 3):  # flat proxy score across the window, never any full_score
        r.db.execute("INSERT INTO turns(round,role,agent,model,started_at) VALUES(?,'implementor','i1','m',?)",
                     (rnd, util.now()))
        tid = r.db.execute("SELECT id FROM turns ORDER BY id DESC LIMIT 1").fetchone()["id"]
        r.db.execute("INSERT INTO results(turn_id,agent,round,proxy_score,at) VALUES(?,'i1',?,0.5,?)",
                     (tid, rnd, util.now()))
    r.db.commit()
    reason = scheduler.termination_reason(r, 3)
    assert reason is not None and "stall" in reason.lower()


def test_board_lock_check_fails_closed_when_locks_missing(run_dir, tmp_repo):
    """run_round must not treat a missing locks.json (tamper-check can't run) as 'not tampered'."""
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    r = Run(run_dir); r.set("run_started_at", util.now())
    # deliberately skip locks.write_locks(r): locks.json does not exist
    with pytest.raises(CollabError):
        scheduler.run_round(r, 1, launch=fake_launch_factory({}))
