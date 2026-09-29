import copy
from pathlib import Path
from swarmcollab.manifest import validate_manifest


def test_valid_manifest_passes(base_manifest, tmp_repo):
    assert validate_manifest(base_manifest, tmp_repo) == []


def test_missing_goal_reported(base_manifest, tmp_repo):
    m = copy.deepcopy(base_manifest); del m["goal"]
    errs = validate_manifest(m, tmp_repo)
    assert any("goal" in e for e in errs)


def test_zero_implementors_reported(base_manifest, tmp_repo):
    m = copy.deepcopy(base_manifest); m["roles"]["implementor"] = 0
    assert any("implementor" in e for e in validate_manifest(m, tmp_repo))


def test_pilot_rounds_exceeding_budget_reported(base_manifest, tmp_repo):
    m = copy.deepcopy(base_manifest); m["pilot"]["rounds"] = 99
    assert any("pilot" in e for e in validate_manifest(m, tmp_repo))


def test_missing_locked_file_reported(base_manifest, tmp_repo):
    m = copy.deepcopy(base_manifest); m["locked_files"] = ["does_not_exist.py"]
    assert any("does_not_exist.py" in e for e in validate_manifest(m, tmp_repo))
