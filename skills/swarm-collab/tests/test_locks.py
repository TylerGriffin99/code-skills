import json
from pathlib import Path
from swarmcollab.model import Run
from swarmcollab import board, locks


def _seed_control(run_dir):
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    roles = run_dir.parent / "roles"  # not used by hash; keep control set minimal for the test


def test_locked_file_change_detected(run_dir, tmp_repo):
    # add a tracked, locked file to the repo baseline
    (tmp_repo / "scorer.py").write_text("GOLDEN = 1\n")
    import subprocess
    subprocess.run(["git", "-C", str(tmp_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_repo), "commit", "-q", "-m", "scorer"], check=True)
    commit = subprocess.run(["git", "-C", str(tmp_repo), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    m = json.loads((run_dir / "swarm.json").read_text())
    m["baseline_commit"] = commit; m["locked_files"] = ["scorer.py"]
    (run_dir / "swarm.json").write_text(json.dumps(m))
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")

    r = Run(run_dir)
    locks.write_locks(r)
    # worktree == repo for this test; unchanged file passes
    assert locks.check_target_locks(r, tmp_repo) == []
    (tmp_repo / "scorer.py").write_text("GOLDEN = 999\n")  # tamper
    assert locks.check_target_locks(r, tmp_repo) == ["scorer.py"]


def test_tamper_with_prior_board_entry_detected(run_dir):
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    r = Run(run_dir)
    board.append(r, round=1, role="strategist", author_agent="s1",
                 type="proposal", body={"text": "A"})
    locks.write_locks(r); locks.advance_board_prefix(r)
    assert locks.check_board_append_only(r) is None
    # rewrite history: overwrite the file with a changed first line
    (run_dir / "board.jsonl").write_text('{"id":1,"type":"proposal","body":{"text":"HACKED"}}\n')
    assert locks.check_board_append_only(r) is not None


def test_new_board_entries_do_not_trip_append_only(run_dir):
    (run_dir / "verdict.md").write_text("VERDICT: GO\n")
    r = Run(run_dir)
    board.append(r, round=1, role="s", author_agent="s1", type="proposal", body={"text": "A"})
    locks.write_locks(r); locks.advance_board_prefix(r)
    board.append(r, round=1, role="c", author_agent="c1", type="critique", body={"text": "ok"})
    assert locks.check_board_append_only(r) is None  # appends are fine
