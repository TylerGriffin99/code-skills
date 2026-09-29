from swarmcollab.model import Run
from swarmcollab import util


def test_run_meta_roundtrip(run_dir):
    r = Run(run_dir)
    assert r.get("phase") is None
    r.set("phase", "pilot")
    assert r.get("phase") == "pilot"
    assert Run(run_dir).get("phase") == "pilot"  # persisted


def test_run_event_writes_log_and_row(run_dir):
    r = Run(run_dir)
    r.event("start", "hello", agent="strategist-1")
    rows = r.db.execute("SELECT kind, agent, detail FROM events").fetchall()
    assert (rows[0]["kind"], rows[0]["agent"], rows[0]["detail"]) == ("start", "strategist-1", "hello")
    assert "hello" in (run_dir / "orchestrator.log").read_text()


def test_render_leaves_unknown_braces():
    assert util.render("hi {name} {unknown}", {"name": "x"}) == "hi x {unknown}"


def test_budget_override_merges(run_dir):
    r = Run(run_dir)
    assert r.budget()["max_rounds"] == 3
    r.set("budget_override", {"max_rounds": 9})
    assert r.budget()["max_rounds"] == 9
