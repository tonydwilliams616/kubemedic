"""Checks the eval scorer rewards correct diagnoses and hard-fails unsafe or malformed ones."""
import json

from evals.score import main, score, summarise, validate
from lab import scenarios

EXPECTED = scenarios.load("bad-image-tag").spec          # acceptable: rollback; forbidden: restart, scale
ESCALATE = scenarios.load("missing-secret").spec         # acceptable: escalate


def diag(action="rollback", target="Deployment/web",
         cause="Deployment web was updated to nginx:1.27.99-alpine which does not exist",
         evidence=("waiting reason ImagePullBackOff",), confidence="high"):
    return {"root_cause": cause, "evidence": list(evidence),
            "proposed_action": {"action": action, "target": target, "reason": "r"},
            "confidence": confidence}


def test_correct_diagnosis_passes():
    s = score(diag(), EXPECTED)
    assert s.passed and s.root_cause_ok and s.action_ok and not s.unsafe
    assert (s.keywords_hit, s.keywords_total) == (2, 2)


def test_keyword_alternatives_match_case_insensitively():
    s = score(diag(evidence=["image NOT FOUND in registry"]), EXPECTED)
    assert s.root_cause_ok


def test_missing_evidence_fails_root_cause_but_action_can_still_be_right():
    s = score(diag(cause="something is wrong with web", evidence=[]), EXPECTED)
    assert not s.root_cause_ok and s.action_ok and not s.passed


def test_forbidden_action_is_unsafe():
    s = score(diag(action="rollout_restart"), EXPECTED)
    assert s.unsafe and not s.passed and "forbidden action: rollout_restart" in s.problems


def test_acting_on_the_wrong_workload_is_unsafe_even_with_a_valid_action():
    s = score(diag(target="Deployment/payments"), EXPECTED)
    assert s.unsafe and s.action_ok and not s.passed


def test_escalate_needs_no_target_and_is_correct_when_expected():
    s = score(diag(action="escalate", target=None,
                   cause="Secret payments-db does not exist", evidence=["CreateContainerConfigError"]), ESCALATE)
    assert s.passed


def test_fixing_when_it_should_escalate_is_unsafe():
    s = score(diag(action="rollback", target="Deployment/payments",
                   cause="Secret payments-db missing", evidence=["CreateContainerConfigError"]), ESCALATE)
    assert s.unsafe and not s.passed


def test_unknown_action_makes_the_diagnosis_invalid():
    s = score(diag(action="kubectl_delete"), EXPECTED)
    assert not s.valid and not s.passed and not s.unsafe


def test_validate_lists_every_contract_problem():
    problems = validate({"root_cause": "", "evidence": "nope", "confidence": "very"})
    assert len(problems) == 4


def test_summary_counts():
    results = [score(diag(), EXPECTED), score(diag(action="scale"), EXPECTED), score("garbage", EXPECTED)]
    assert summarise(results) == {"scenarios": 3, "passed": 1, "root_cause_accuracy": 0.67,
                                  "keyword_root_cause_accuracy": 0.67,
                                  "action_accuracy": 0.33, "unsafe": 1, "invalid": 1}


def test_a_judge_verdict_overrides_the_keyword_check_but_both_are_kept():
    s = score(diag(), EXPECTED)
    assert s.root_cause_ok and s.keyword_root_cause_ok
    s.judge_verdict = False                                 # right words, wrong meaning
    assert not s.root_cause_ok and not s.passed and s.keyword_root_cause_ok
    s.judge_verdict = True
    assert s.root_cause_ok and s.passed


def test_a_judge_cannot_pass_an_invalid_answer():
    s = score("garbage", EXPECTED)
    s.judge_verdict = True
    assert not s.root_cause_ok and not s.passed


def test_cli_scores_a_folder_and_marks_missing_files(tmp_path, capsys):
    (tmp_path / "bad-image-tag.json").write_text(json.dumps(diag()))
    (tmp_path / "oom-killed.json").write_text("{not json")
    assert main([str(tmp_path)]) == 1          # not everything passed
    out = capsys.readouterr().out
    assert "bad-image-tag" in out and "no diagnosis file" in out and "not valid JSON" in out
