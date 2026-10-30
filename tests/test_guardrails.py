"""Checks the guardrails block actions the evidence doesn't support, and that the diagnoser repairs or fails closed."""
import json

from kubemedic import guardrails
from kubemedic.diagnose import diagnose


def bundle(*revisions):
    """An evidence bundle with one Deployment called 'reports' and the given (revision, current) pairs."""
    return {"deployments": [{"name": "reports", "rollout_history": [
        {"revision": rev, "current": cur} for rev, cur in revisions]}]}


def answer(action, target="Deployment/reports"):
    act = {"action": action, "reason": "r"} | ({"target": target} if target else {})
    return {"root_cause": "CPU request of 64 should be 64m", "evidence": ["Insufficient cpu"],
            "proposed_action": act, "confidence": "high"}


ONE_REVISION = bundle((1, True))
TWO_REVISIONS = bundle((1, False), (2, True))


# ---------- the rules ----------

def test_rollback_with_no_earlier_revision_is_blocked():
    assert "needs an earlier revision" in guardrails.check(answer("rollback"), ONE_REVISION)[0]


def test_rollback_with_an_earlier_revision_is_allowed():
    assert guardrails.check(answer("rollback"), TWO_REVISIONS) == []


def test_unknown_target_is_blocked():
    assert "not a Deployment in the evidence" in guardrails.check(answer("rollout_restart", "Deployment/payments"), ONE_REVISION)[0]
    assert guardrails.check(answer("scale", "StatefulSet/reports"), ONE_REVISION)


def test_escalate_is_always_allowed():
    assert guardrails.check(answer("escalate", target=None), {}) == []


# ---------- what the diagnoser does when a rule is broken ----------

class Scripted:
    model_id = "fake"

    def __init__(self, *replies):
        self.replies = [json.dumps(r) for r in replies]
        self.requests = []

    def converse(self, system, messages):
        self.requests.append(json.loads(json.dumps(messages)))
        return self.replies.pop(0), {"input_tokens": 10, "output_tokens": 5}


def test_model_gets_one_chance_to_fix_a_blocked_action():
    model = Scripted(answer("rollback"), answer("escalate", target=None))
    r = diagnose(ONE_REVISION, model)
    assert r.valid and not r.overridden and r.diagnosis["proposed_action"]["action"] == "escalate"
    assert len(r.guardrail_violations) == 1                       # the block is still recorded
    assert "needs an earlier revision" in model.requests[1][-1]["content"][0]["text"]   # and explained to the model


def test_if_the_model_insists_the_system_fails_closed_to_escalate():
    r = diagnose(ONE_REVISION, Scripted(answer("rollback"), answer("rollback")))
    assert r.valid and r.overridden
    assert r.diagnosis["proposed_action"]["action"] == "escalate"
    assert "guardrail blocked" in r.diagnosis["proposed_action"]["reason"]
    assert r.diagnosis["root_cause"] == "CPU request of 64 should be 64m"   # the diagnosis itself is kept
    assert len(r.guardrail_violations) == 2


def test_a_supported_rollback_passes_untouched():
    r = diagnose(TWO_REVISIONS, Scripted(answer("rollback")))
    assert r.valid and r.attempts == 1 and r.guardrail_violations == [] and not r.overridden
