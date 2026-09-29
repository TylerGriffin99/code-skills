import json
import subprocess
from pathlib import Path
import sys

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


@pytest.fixture
def tmp_repo(tmp_path):
    """A real git repo with one commit, so worktree/locking code has a baseline."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)
    return repo


@pytest.fixture
def base_manifest(tmp_repo):
    commit = subprocess.run(["git", "-C", str(tmp_repo), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    return {
        "run_id": "toy-L01", "name": "toy", "repo": str(tmp_repo), "workdir": ".",
        "baseline_commit": commit, "maximize": True,
        "goal": "drive the number to the target",
        "roles": {"strategist": 1, "implementor": 2, "critic": 1},
        "locked_files": [], "strip_env": [],
        "budget": {"max_usd": 1.0, "max_rounds": 3, "max_hours": 1,
                   "round_token_cap": 100000, "agent_token_cap": 20000},
        "view": {"token_ceiling": 4000, "compact_threshold_entries": 50},
        "messaging": {"per_turn_quota": 2, "max_tokens": 200},
        "eval": {"proxy_command": "true", "full_command": "true", "verify_tolerance": 0.0},
        "rounds": {"strategist_every": 1, "critic_every": 1, "council_every": 1},
        "pilot": {"rounds": 1},
    }


@pytest.fixture
def run_dir(tmp_path, base_manifest):
    d = tmp_path / "run"
    d.mkdir()
    (d / "swarm.json").write_text(json.dumps(base_manifest))
    return d
