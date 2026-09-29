"""The Run ledger: manifest access, sqlite event/turn log, and budget resolution."""

import json
import sqlite3
import sys
from pathlib import Path

from swarmcollab.util import CollabError, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, agent TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT, round INTEGER NOT NULL, role TEXT NOT NULL,
  agent TEXT NOT NULL, model TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
  exit_code INTEGER, is_error INTEGER, timed_out INTEGER, cost_usd REAL, tokens INTEGER,
  worktree TEXT, commit_before TEXT, commit_after TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, turn_id INTEGER NOT NULL, agent TEXT NOT NULL,
  round INTEGER NOT NULL, proxy_score REAL, full_score REAL, promoted INTEGER DEFAULT 0,
  commit_sha TEXT, board_entry_id INTEGER, at TEXT NOT NULL
);
"""


class Run:
    def __init__(self, run_dir: Path):
        self.dir = Path(run_dir).expanduser().resolve()
        manifest = self.dir / "swarm.json"
        if not manifest.is_file():
            raise CollabError(f"not a collab run dir (no swarm.json): {self.dir}")
        self.m = json.loads(manifest.read_text())
        self.db = sqlite3.connect(str(self.dir / "ledger.sqlite"), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.repo = Path(self.m["repo"])
        self.run_id = self.m["run_id"]
        self.lanes_root = self.repo.parent / f"{self.repo.name}-collab-{self.run_id}"

    # meta
    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, json.dumps(value)))
        self.db.commit()

    def event(self, kind, detail, agent=None):
        self.db.execute("INSERT INTO events(at, kind, agent, detail) VALUES(?,?,?,?)", (now(), kind, agent, detail))
        self.db.commit()
        self.log(f"[{kind}]{' ' + agent if agent else ''} {detail}")

    def log(self, message):
        line = f"[{now()}] {message}"
        with open(self.dir / "orchestrator.log", "a") as fh:
            fh.write(line + "\n")
        print(line, file=sys.stderr, flush=True)

    # budget (scale may override the manifest's)
    def budget(self):
        b = dict(self.m["budget"])
        b.update(self.get("budget_override", {}))
        return b
