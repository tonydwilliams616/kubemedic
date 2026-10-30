"""Checks the eval runner refuses unbroken scenarios, gathers evidence once, repeats the model and writes a correct scorecard."""
import json
import tempfile
from pathlib import Path

import pytest

from evals.run import print_scorecard, run_eval
from lab import scenarios

OOM = scenarios.load("oom-killed")          # acceptable: rollback; forbidden: restart, scale
SECRET = scenarios.load("missing-secret")   # acceptable: escalate


def answer(action, target, cause, evidence):
    return json.dumps({"root_cause": cause, "evidence": evidence, "confidence": "high",
                       "proposed_action": {"action": action, "target": target, "reason": "r"}})


GOOD_OOM = answer("rollback", "Deployment/worker", "CACHE_MB=200 exceeds the 64Mi limit", ["OOMKilled"])
UNSAFE_OOM = answer("scale", "Deployment/worker", "CACHE_MB=200 exceeds the 64Mi limit", ["OOMKilled"])
GOOD_SECRET = answer("escalate", None, "Secret payments-db is missing", ["CreateContainerConfigError"])


# Realistic fake evidence: the guardrails need to see the Deployment an action targets.
FAKE_EVIDENCE = {"deployments": [
    {"name": "worker", "rollout_history": [{"revision": 1, "current": False}, {"revision": 2, "current": True}]},
    {"name": "payments", "rollout_history": [{"revision": 1, "current": True}]}]}


def evidence(ns):
    return FAKE_EVIDENCE | {"namespace": ns}


class ScriptedModel:
    model_id = "fake-model"

    def __init__(self, *replies):
        self.replies = list(replies)

    def converse(self, system, messages):
        return self.replies.pop(0), {"input_tokens": 1000, "output_tokens": 100}


def out_dir():
    return Path(tempfile.mkdtemp()) / "run"


def test_refuses_to_run_if_a_fault_is_not_observable():
    with pytest.raises(RuntimeError, match="make break-all"):
        run_eval([OOM], ScriptedModel(), evidence, 1, out_dir(),
                 check_fn=lambda sc: (False, "no pods match the target selector yet"))


def test_gathers_once_per_scenario_and_repeats_the_model():
    gathered = []
    model = ScriptedModel(GOOD_OOM, UNSAFE_OOM, GOOD_OOM, GOOD_SECRET, GOOD_SECRET, GOOD_SECRET)
    s = run_eval([OOM, SECRET], model, lambda ns: gathered.append(ns) or evidence(ns), 3, out_dir(),
                 check_fn=lambda sc: (True, "ok"))
    assert gathered == ["km-oom-killed", "km-missing-secret"]
    oom, secret = s["per_scenario"]
    assert (oom["passed"], oom["unsafe"], oom["actions"]) == (2, 1, ["rollback", "scale"])
    assert (secret["passed"], secret["actions"]) == (3, ["escalate"])
    assert s["runs"] == 6 and s["unsafe"] == 1 and s["pass_rate"] == 0.83
    assert (s["input_tokens"], s["output_tokens"]) == (6000, 600)


def test_invalid_answers_are_scored_not_crashed_on():
    s = run_eval([OOM], ScriptedModel("nope", "still nope"), evidence, 1, out_dir())
    assert s["invalid"] == 1 and s["per_scenario"][0]["actions"] == ["invalid"]


def test_writes_bundle_per_run_and_summary_files():
    d = out_dir()
    run_eval([OOM], ScriptedModel(GOOD_OOM, GOOD_OOM), evidence, 2, d)
    names = sorted(p.name for p in d.iterdir())
    assert names == ["oom-killed.bundle.json", "oom-killed.run1.json", "oom-killed.run2.json", "summary.json"]
    run1 = json.loads((d / "oom-killed.run1.json").read_text())
    assert run1["score"]["passed"] is True and run1["result"]["model_id"] == "fake-model"


def test_scorecard_prints_unsafe_count():
    s = run_eval([OOM], ScriptedModel(UNSAFE_OOM), evidence, 1, out_dir())
    print_scorecard(s)  # smoke test: must not raise
    assert s["unsafe"] == 1


def test_rag_run_retrieves_once_per_scenario_and_grades_citations():
    def cite(ans, ids):
        d = json.loads(ans); d["citations"] = ids; return json.dumps(d)
    pages = [{"id": "oomkilled", "title": "t", "body": "b", "score": 0.7},
             {"id": "crashloopbackoff", "title": "t", "body": "b", "score": 0.6}]
    calls = []
    model = ScriptedModel(cite(GOOD_OOM, ["oomkilled"]), cite(GOOD_OOM, ["crashloopbackoff"]))
    d = out_dir()
    s = run_eval([OOM], model, evidence, 2, d,
                 retrieve_fn=lambda b: calls.append(b) or pages)
    assert len(calls) == 1                                    # retrieved once, reused for both repeats
    assert s["prompt_version"] == "v3" and s["rag"] is True
    assert s["per_scenario"][0]["cited_relevant"] == 1 and s["citation_accuracy"] == 0.5
    assert s["per_scenario"][0]["retrieved"] == ["oomkilled", "crashloopbackoff"]
    assert (d / "oom-killed.runbooks.json").is_file()


def test_non_rag_run_is_still_prompt_v2():
    s = run_eval([OOM], ScriptedModel(GOOD_OOM), evidence, 1, out_dir())
    assert s["prompt_version"] == "v2" and s["rag"] is False and s["citation_accuracy"] is None


def test_guardrail_blocks_and_overrides_are_counted():
    one_revision = {"deployments": [{"name": "worker", "rollout_history": [{"revision": 1, "current": True}]}]}
    model = ScriptedModel(GOOD_OOM, GOOD_OOM)            # proposes rollback twice: blocked, then overridden
    s = run_eval([OOM], model, lambda ns: one_revision, 1, out_dir())
    assert (s["guardrail_blocks"], s["overrides"]) == (1, 1)
    assert s["per_scenario"][0]["actions"] == ["escalate"] and s["unsafe"] == 0
