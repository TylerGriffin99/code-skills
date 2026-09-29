import json
from swarmcollab.model import Run
from swarmcollab import evaluate, util
import pytest


def _set_eval(run_dir, proxy, full, tol=0.0, promote=None):
    m = json.loads((run_dir / "swarm.json").read_text())
    m["eval"] = {"proxy_command": proxy, "full_command": full, "verify_tolerance": tol}
    if promote is not None:
        m["eval"]["promote_threshold"] = promote
    (run_dir / "swarm.json").write_text(json.dumps(m))


def test_eval_reads_numeric_score(run_dir, tmp_repo):
    _set_eval(run_dir, proxy="""python3 -c 'print(\"{\\\"score\\\": 0.42}\")'""",
              full="""python3 -c 'print(\"{\\\"score\\\": 0.9}\")'""")
    r = Run(run_dir)
    out = evaluate.run_eval(r, "proxy_command", tmp_repo, "HEAD", "impl-1")
    assert out["score"] == 0.42


def test_eval_no_json_raises(run_dir, tmp_repo):
    _set_eval(run_dir, proxy="echo not-json", full="true")
    r = Run(run_dir)
    with pytest.raises(util.CollabError):
        evaluate.run_eval(r, "proxy_command", tmp_repo, "HEAD", "impl-1")


def test_agent_reported_score_is_ignored(run_dir, tmp_repo):
    # proxy says 0.10 regardless of what an agent claims
    _set_eval(run_dir, proxy="""python3 -c 'print(\"{\\\"score\\\": 0.10}\")'""",
              full="""python3 -c 'print(\"{\\\"score\\\": 0.10}\")'""", promote=1.0)
    r = Run(run_dir)
    r.db.execute("INSERT INTO turns(round, role, agent, model, started_at) VALUES(1,'implementor','impl-1','m',?)",
                 (util.now(),)); r.db.commit()
    turn_id = r.db.execute("SELECT id FROM turns").fetchone()["id"]
    out = evaluate.score_result(r, worktree=tmp_repo, commit="HEAD", agent="impl-1",
                                round=1, turn_id=turn_id)
    assert out["proxy_score"] == 0.10 and out["promoted"] is False


def test_eval_within_tolerance_ok(run_dir, tmp_repo):
    _set_eval(run_dir, proxy="true",
              full="""python3 -c 'print(\"{\\\"score\\\": 0.5001}\")'""", tol=0.01)
    r = Run(run_dir)
    ok, val = evaluate.verify(r, worktree=tmp_repo, commit="HEAD", recorded=0.5)
    assert ok is True
