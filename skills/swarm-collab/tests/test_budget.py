from swarmcollab.model import Run
from swarmcollab import budget, util


def _turn(r, round, agent, cost, tokens):
    r.db.execute(
        "INSERT INTO turns(round, role, agent, model, started_at, ended_at, cost_usd, tokens)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (round, "implementor", agent, "m", util.now(), util.now(), cost, tokens))
    r.db.commit()


def test_totals_sum(run_dir):
    r = Run(run_dir)
    _turn(r, 1, "impl-1", 0.10, 500); _turn(r, 1, "impl-2", 0.20, 700)
    t = budget.totals(r)
    assert round(t["cost"], 2) == 0.30 and t["tokens"] == 1200


def test_budget_reason_on_cost(run_dir):
    r = Run(run_dir); r.set("run_started_at", util.now())
    _turn(r, 1, "impl-1", 1.5, 100)  # max_usd is 1.0
    assert "budget" in budget.budget_reason(r, 1)


def test_budget_reason_on_rounds(run_dir):
    r = Run(run_dir); r.set("run_started_at", util.now())
    assert budget.budget_reason(r, 3) is not None  # max_rounds == 3
    assert budget.budget_reason(r, 1) is None


def test_round_cap(run_dir):
    r = Run(run_dir); r.set("run_started_at", util.now())
    _turn(r, 1, "impl-1", 0.0, 100000)  # round_token_cap is 100000
    assert budget.round_cap_reason(r, 1) is not None
