import json

from swarmcollab.model import Run
from swarmcollab import reports, util


def _set_full_command(run_dir, full_cmd, tol=0.01):
    m = json.loads((run_dir / "swarm.json").read_text())
    m["eval"] = {"proxy_command": "true", "full_command": full_cmd, "verify_tolerance": tol}
    (run_dir / "swarm.json").write_text(json.dumps(m))


def _promoted_result(r, commit, score):
    r.db.execute("INSERT INTO turns(round,role,agent,model,started_at) VALUES(1,'implementor','impl-1','m',?)",
                 (util.now(),))
    tid = r.db.execute("SELECT id FROM turns ORDER BY id DESC LIMIT 1").fetchone()["id"]
    r.db.execute(
        "INSERT INTO results(turn_id,agent,round,proxy_score,full_score,promoted,commit_sha,at)"
        " VALUES(?,?,?,?,?,1,?,?)",
        (tid, "impl-1", 1, score, score, commit, util.now()),
    )
    r.db.commit()


def test_harvest_verifies_promoted_result_and_surfaces_it(run_dir, tmp_repo):
    _set_full_command(run_dir, "python3 -c 'print(\"{\\\"score\\\": 0.5}\")'")
    commit = util.git(tmp_repo, "rev-parse", "HEAD")
    r = Run(run_dir)
    _promoted_result(r, commit, 0.5)
    out = reports.harvest(r)
    assert "Verification" in out          # the section is present
    assert "pass" in out                  # re-run matched the recorded score within tolerance
    assert "0.5" in out                   # the re-run number is surfaced


def test_harvest_reports_verify_error_without_crashing(run_dir, tmp_repo):
    # full eval exits non-zero -> evaluate.verify raises; harvest must not crash.
    _set_full_command(run_dir, "false")
    commit = util.git(tmp_repo, "rev-parse", "HEAD")
    r = Run(run_dir)
    _promoted_result(r, commit, 0.5)
    out = reports.harvest(r)
    assert "ERROR" in out
