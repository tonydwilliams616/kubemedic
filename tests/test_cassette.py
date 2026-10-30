"""Checks a recorded eval replays to identical scores with no model or cluster, that any change to the request forces a re-record, and that the merge gate passes and fails correctly."""
import json
import tempfile
from pathlib import Path

import pytest

from evals import gate
from evals import run as run_module
from evals.cassette import NotRecorded, Player, Recorder, fingerprint
from evals.run import run_eval
from kubemedic.runbooks import load_all as load_runbooks
from lab import scenarios

OOM = scenarios.load("oom-killed")
SECRET = scenarios.load("missing-secret")


def answer(action, target, cause, citations, evidence):
    return json.dumps({"root_cause": cause, "evidence": evidence, "confidence": "high", "citations": citations,
                       "proposed_action": {"action": action, "target": target, "reason": "r"}})


GOOD_OOM = answer("rollback", "Deployment/worker", "CACHE_MB=200 exceeds the 64Mi limit", ["oomkilled"], ["OOMKilled"])
BAD_OOM = answer("scale", "Deployment/worker", "CACHE_MB=200 exceeds the 64Mi limit", ["oomkilled"], ["OOMKilled"])
GOOD_SECRET = answer("escalate", None, "Secret payments-db is missing", ["createcontainerconfigerror"], ["CreateContainerConfigError"])


class FakeBedrock:
    """Answers by namespace and counts calls, standing in for the real model during recording."""
    model_id = "fake-model"

    def __init__(self, replies):
        self.replies, self.calls = {k: list(v) for k, v in replies.items()}, 0

    def converse(self, system, messages):
        self.calls += 1
        ns = "km-oom-killed" if "km-oom-killed" in messages[0]["content"][0]["text"] else "km-missing-secret"
        return self.replies[ns].pop(0), {"input_tokens": 100, "output_tokens": 10}


def evidence(ns):
    return {"namespace": ns, "deployments": [
        {"name": "worker", "rollout_history": [{"revision": 1, "current": False}, {"revision": 2, "current": True}]},
        {"name": "payments", "rollout_history": [{"revision": 1, "current": True}]}]}


RUNBOOKS = {rb.id: rb for rb in load_runbooks()}


def pages(bundle):
    """Fake search returning real pages: replay reloads page text from runbooks/, and any difference would be a prompt change."""
    rb = RUNBOOKS["oomkilled" if bundle["namespace"] == "km-oom-killed" else "createcontainerconfigerror"]
    return [{"id": rb.id, "title": rb.title, "score": 0.5, "body": rb.body}]


def test_an_edited_runbook_needs_a_rerecord():
    tmp = Path(tempfile.mkdtemp())
    rec = Recorder(FakeBedrock({}), tmp / "rec")
    rec.retrieving(lambda b: [{"id": "oomkilled", "title": "old title", "score": 0.5, "body": "old text"}])({"namespace": "km-oom-killed"})
    player = Player.__new__(Player)
    player.path = rec.path
    page = player.retrieve({"namespace": "km-oom-killed"})[0]
    assert page["body"] == RUNBOOKS["oomkilled"].body   # today's text, so the prompt will no longer match


def record(tmp, replies):
    """Record a two-scenario, two-repeat RAG eval; returns (summary, recording dir)."""
    rec = Recorder(FakeBedrock(replies), tmp / "rec")
    summary = run_eval([OOM, SECRET], rec, rec.gathering(evidence), 2, tmp / "out", None, rec.retrieving(pages))
    rec.save(summary)
    return summary, tmp / "rec"


def replay(rec_dir, tmp):
    player = Player(rec_dir)
    return run_eval([OOM, SECRET], player, player.gather, 2, tmp / "replayed", None, player.retrieve)


def test_replay_reproduces_the_recorded_scores_without_the_model():
    tmp = Path(tempfile.mkdtemp())
    # Repeats of the SAME request get DIFFERENT answers; replay must hand them back in order.
    summary, rec_dir = record(tmp, {"km-oom-killed": [GOOD_OOM, BAD_OOM, GOOD_OOM], "km-missing-secret": [GOOD_SECRET] * 2})
    replayed = replay(rec_dir, tmp)
    for key in ("pass_rate", "action_accuracy", "unsafe", "citation_accuracy", "input_tokens", "guardrail_blocks"):
        assert replayed[key] == summary[key], key
    assert not (tmp / "rec.partial").exists() and (rec_dir / "meta.json").is_file()
    assert json.loads((rec_dir / "meta.json").read_text())["scenarios"] == ["oom-killed", "missing-secret"]


def test_any_change_to_the_request_needs_a_rerecord():
    tmp = Path(tempfile.mkdtemp())
    _, rec_dir = record(tmp, {"km-oom-killed": [GOOD_OOM] * 2, "km-missing-secret": [GOOD_SECRET] * 2})
    player = Player(rec_dir)
    with pytest.raises(NotRecorded, match="re-record"):
        player.converse("a changed system prompt", [{"role": "user", "content": [{"text": "x"}]}])


def test_replaying_more_calls_than_recorded_fails():
    player = Player.__new__(Player)
    key = fingerprint("m", "s", [])
    player.model_id, player.calls, player._used = "m", {key: [{"text": "t", "usage": {}}]}, {}
    assert player.converse("s", [])[0] == "t"
    with pytest.raises(NotRecorded, match="more times than recorded"):
        player.converse("s", [])


def test_a_failed_recording_keeps_the_old_one():
    tmp = Path(tempfile.mkdtemp())
    _, rec_dir = record(tmp, {"km-oom-killed": [GOOD_OOM] * 2, "km-missing-secret": [GOOD_SECRET] * 2})
    before = (rec_dir / "calls.json").read_text()
    Recorder(FakeBedrock({}), rec_dir)        # starts a new recording, which then never calls save()
    assert (rec_dir / "calls.json").read_text() == before


def test_missing_recording_is_a_clear_error():
    with pytest.raises(NotRecorded, match="make record"):
        Player(Path(tempfile.mkdtemp()) / "nothing")


def test_gate_rules():
    rules = {"pass_rate": {"min": 0.9}, "unsafe": {"max": 0}, "citation_accuracy": {"min": 0.9}}
    assert gate.check({"pass_rate": 0.94, "unsafe": 0, "citation_accuracy": 1.0}, rules) == []
    failures = gate.check({"pass_rate": 0.89, "unsafe": 1}, rules)
    assert len(failures) == 3 and "not measured" in failures[2]   # a metric that vanished fails too


def test_committed_gate_file_is_valid():
    rules = gate.load()
    assert rules["unsafe"] == {"max": 0} and all(set(r) <= {"min", "max"} for r in rules.values())


def test_cli_replay_with_gate_passes_and_fails():
    tmp = Path(tempfile.mkdtemp())
    saved, run_module.REPORTS_DIR = run_module.REPORTS_DIR, tmp / "reports"   # keep test reports out of the repo
    try:
        _cli_replay_checks(tmp)
    finally:
        run_module.REPORTS_DIR = saved


def _cli_replay_checks(tmp):
    _, good = record(tmp, {"km-oom-killed": [GOOD_OOM] * 2, "km-missing-secret": [GOOD_SECRET] * 2})
    args = ["--replay", str(good), "--gate", str(gate.GATE_FILE), "--scenario", "oom-killed", "--scenario", "missing-secret"]
    assert run_module.main(args) == 0

    bad_tmp = Path(tempfile.mkdtemp())
    _, bad = record(bad_tmp, {"km-oom-killed": [BAD_OOM] * 4, "km-missing-secret": [GOOD_SECRET] * 2})
    args[1] = str(bad)
    assert run_module.main(args) == 1
