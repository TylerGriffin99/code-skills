import pytest
from swarmcollab.model import Run
from swarmcollab import board


def test_append_assigns_monotonic_ids(run_dir):
    r = Run(run_dir)
    e1 = board.append(r, round=1, role="strategist", author_agent="s1",
                      type="proposal", body={"text": "try A"})
    e2 = board.append(r, round=1, role="strategist", author_agent="s1",
                      type="proposal", body={"text": "try B"})
    assert e1["id"] == 1 and e2["id"] == 2
    assert board.max_id(r) == 2


def test_read_filters_by_type_and_refs(run_dir):
    r = Run(run_dir)
    p = board.append(r, round=1, role="strategist", author_agent="s1",
                     type="proposal", body={"text": "A"})
    board.append(r, round=1, role="critic", author_agent="c1",
                 type="critique", body={"text": "risky"}, refs=[p["id"]])
    assert [e["type"] for e in board.read(r, types=["proposal"])] == ["proposal"]
    assert board.read(r, refs=[p["id"]])[0]["type"] == "critique"


def test_read_to_and_since(run_dir):
    r = Run(run_dir)
    board.append(r, round=1, role="strategist", author_agent="s1",
                 type="message", body={"text": "hi impl-1"}, to=["impl-1"])
    assert board.read(r, to=["impl-1"])[0]["body"]["text"] == "hi impl-1"
    assert board.read(r, since_id=99) == []


def test_append_rejects_unknown_type(run_dir):
    r = Run(run_dir)
    with pytest.raises(Exception):
        board.append(r, round=1, role="x", author_agent="x", type="bogus", body={})
