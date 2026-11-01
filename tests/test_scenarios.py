"""Every scenario in the repo must pass validation, and the validator must catch bad ones."""
import shutil

import pytest
import yaml

from lab import scenarios


@pytest.mark.parametrize("s", scenarios.load_all(), ids=lambda s: s.id)
def test_repo_scenarios_are_valid(s):
    assert scenarios.validate(s) == []


def test_there_are_seven_scenarios_covering_distinct_categories():
    all_s = scenarios.load_all()
    assert len(all_s) == 7
    assert len({s.spec["category"] for s in all_s}) == 7


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


def test_the_failing_deployment_may_be_defined_only_in_base_yaml(tmp_path):
    # service-selector-mismatch breaks a Service; the failing Deployment exists only in base.yaml.
    _copy(tmp_path, "service-selector-mismatch")
    assert scenarios.validate(scenarios.load("service-selector-mismatch", tmp_path)) == []


def test_a_target_in_neither_file_is_rejected(tmp_path):
    d = _copy(tmp_path, "service-selector-mismatch")
    _edit(d, target={"kind": "Deployment", "name": "nope", "selector": "app=nope"})
    errors = scenarios.validate(scenarios.load("service-selector-mismatch", tmp_path))
    assert any("not found in break.yaml or base.yaml" in e for e in errors)
