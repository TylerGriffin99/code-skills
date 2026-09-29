import json, os, sys
from pathlib import Path
from swarmcollab import cli
from swarmcollab.model import Run

SKILL_DIR = Path(__file__).resolve().parent.parent


def test_init_refuses_without_go(run_dir, capsys):
    # no verdict.md → init must fail
    setup = run_dir  # reuse: swarm.json present, no verdict.md
    rc = cli.main(["init", str(setup)])
    assert rc != 0


def test_e2e_toy_reaches_target_with_mock_agents(tmp_path, tmp_repo):
    """A toy problem: implementors 'raise the number'; the eval returns round number as score;
    target_score=2 so the run stops by round 2. Uses the mock agent, no real claude, no GPU."""
    setup = tmp_path / "docs" / "swarm-collab" / "toy"
    setup.mkdir(parents=True)
    (setup / "verdict.md").write_text("VERDICT: GO\n")
    commit = os.popen(f"git -C {tmp_repo} rev-parse HEAD").read().strip()
    # eval: score == number of result rows so far (so it climbs each round)
    proxy = (f"python3 -c \"import sqlite3,glob,json,os;"
             f"db=glob.glob(os.environ['COLLAB_RUN_DIR']+'/ledger.sqlite')[0];"
             f"c=sqlite3.connect(db).execute('SELECT COUNT(*) FROM results').fetchone()[0];"
             f"print(json.dumps({{'score': c}}))\"")
    manifest = {
        "name": "toy", "repo": str(tmp_repo), "workdir": ".", "baseline_commit": commit,
        "maximize": True, "goal": "raise the number", "target_score": 2,
        "roles": {"strategist": 1, "implementor": 1, "critic": 0},
        "locked_files": [], "strip_env": [],
        "budget": {"max_usd": 10, "max_rounds": 5, "max_hours": 1,
                   "round_token_cap": 10**9, "agent_token_cap": 10**9},
        "view": {"token_ceiling": 4000, "compact_threshold_entries": 100},
        "messaging": {"per_turn_quota": 1, "max_tokens": 200},
        "eval": {"proxy_command": proxy, "full_command": proxy, "verify_tolerance": 0},
        "rounds": {"strategist_every": 1, "critic_every": 1, "council_every": 99},
        "pilot": {"rounds": 5},
    }
    (setup / "swarm.json").write_text(json.dumps(manifest))
    # scripted mock: strategist proposes, implementor posts a result
    script = setup / "impl_script.jsonl"
    script.write_text(json.dumps({"type": "result", "body": {"text": "bumped"}}))
    os.environ["COLLAB_AGENT_CMD"] = f"{sys.executable} {SKILL_DIR / 'tests' / 'mock_agent.py'}"
    os.environ["COLLAB_MOCK_SCRIPT"] = str(script)
    try:
        assert cli.main(["init", str(setup)]) == 0
        run_dir = sorted((setup).glob("*/swarm.json"))[0].parent
        assert cli.main(["run", str(run_dir)]) == 0
        r = Run(run_dir)
        best = r.db.execute("SELECT MAX(full_score) m FROM results").fetchone()["m"]
        assert best is not None and best >= 2
    finally:
        os.environ.pop("COLLAB_AGENT_CMD", None); os.environ.pop("COLLAB_MOCK_SCRIPT", None)
