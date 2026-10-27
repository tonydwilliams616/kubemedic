"""Every scenario in the repo must pass validation, and the validator must catch bad ones."""
import shutil

import pytest
import yaml

from lab import scenarios


@pytest.mark.parametrize("s", scenarios.load_all(), ids=lambda s: s.id)
def test_repo_scenarios_are_valid(s):
    assert scenarios.validate(s) == []


def test_there_are_six_scenarios_covering_distinct_categories():
    all_s = scenarios.load_all()
    assert len(all_s) == 6
    assert len({s.spec["category"] for s in all_s}) == 6


def _copy(tmp_path, scenario_id):
    dst = tmp_path / scenario_id
    shutil.copytree(scenarios.SCENARIOS_DIR / scenario_id, dst)
    return dst


def _edit(path, **changes):
    spec = yaml.safe_load((path / "expected.yaml").read_text())
    spec.update(changes)
    (path / "expected.yaml").write_text(yaml.safe_dump(spec))


def test_rollback_without_base_is_rejected(tmp_path):
    d = _copy(tmp_path, "missing-secret")
    _edit(d, acceptable_actions=["rollback"], forbidden_actions=[])
    errors = scenarios.validate(scenarios.load("missing-secret", tmp_path))
    assert any("no base.yaml" in e for e in errors)


def test_unknown_action_is_rejected(tmp_path):
    d = _copy(tmp_path, "oom-killed")
    _edit(d, acceptable_actions=["kubectl_exec"])
    assert scenarios.validate(scenarios.load("oom-killed", tmp_path))


def test_hardcoded_namespace_is_rejected(tmp_path):
    d = _copy(tmp_path, "oom-killed")
    docs = list(yaml.safe_load_all((d / "break.yaml").read_text()))
    docs[0]["metadata"]["namespace"] = "default"
    (d / "break.yaml").write_text(yaml.safe_dump_all(docs))
    errors = scenarios.validate(scenarios.load("oom-killed", tmp_path))
    assert any("sets a namespace" in e for e in errors)


def test_overlapping_actions_are_rejected(tmp_path):
    d = _copy(tmp_path, "oom-killed")
    _edit(d, forbidden_actions=["rollback"])
    errors = scenarios.validate(scenarios.load("oom-killed", tmp_path))
    assert any("both acceptable and forbidden" in e for e in errors)
