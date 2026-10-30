"""Checks the agent is scored like the one-shot diagnoser, that a taped agent run replays identically with no cluster or model, that tapes hold only redacted data, and that the CLI gates agent replays."""
import json
import tempfile
from pathlib import Path

import pytest

from evals import gate
from evals import run as run_module
from evals.cassette import NotRecorded, TapePlayer, TapeRecorder
from evals.run import _agent_runner, run_eval
from kubemedic.runbooks import load_all
from lab import scenarios
from test_agent import ScriptedAgentModel, call, diagnosis, kubectl

OOM = scenarios.load("oom-killed")                     # right answer: rollback
UNSCHEDULABLE = scenarios.load("unschedulable-requests")   # right answer: escalate


class FakeRetriever:
    """Switches the runbook tools on; read_runbook serves real pages."""
    runbooks = {rb.id: rb for rb in load_all()}
GOOD = diagnosis() | {"root_cause": "CACHE_MB=200 exceeds the 64Mi memory limit"}
SCRIPT = [[call("list_pods")], [call("get_logs", {"pod": "worker-abc", "previous": True})],
          [call("submit_diagnosis", GOOD)]]


def record(tmp, turns, revisions=(1, 2), repeats=2, scenario=OOM, rag=False):
    tape = TapeRecorder(tmp / "agent")
    model = tape.model(ScriptedAgentModel(*turns))
    summary = run_eval([scenario], model, None, repeats, tmp / "out", agent_rag=rag,
                       investigate_fn=_agent_runner(model, kubectl(revisions), FakeRetriever() if rag else None, tape))
    tape.save(summary)
    return summary, tmp / "agent"


def replay(rec_dir, tmp, repeats=2, scenario=OOM):
    tape = TapePlayer(rec_dir)
    model = tape.model()
    return run_eval([scenario], model, None, repeats, tmp / "replayed", investigate_fn=_agent_runner(model, None, None, tape))


def test_agent_is_scored_with_steps_and_model_vs_system_accuracy():
    summary, _ = record(Path(tempfile.mkdtemp()), SCRIPT * 2)
    assert summary["mode"] == "agent" and summary["prompt_version"] == "a1" and summary["pass_rate"] == 1.0
    assert summary["mean_steps"] == 3 and summary["mean_tool_calls"] == 2 and summary["budget_exhausted"] == 0
    assert summary["model_action_accuracy"] == 1.0 and summary["per_scenario"][0]["mean_steps"] == 3


def test_a_taped_agent_run_replays_identically_without_cluster_or_model():
    tmp = Path(tempfile.mkdtemp())
    summary, rec_dir = record(tmp, SCRIPT * 2)
    replayed = replay(rec_dir, tmp)          # kubectl is None and there is no model: everything comes from the tape
    for key in ("pass_rate", "action_accuracy", "mean_steps", "mean_tool_calls", "input_tokens", "unsafe"):
        assert replayed[key] == summary[key], key
    assert json.loads((rec_dir / "meta.json").read_text())["mode"] == "agent"


def test_guardrail_rescue_is_system_accuracy_not_model_accuracy_and_replays():
    tmp = Path(tempfile.mkdtemp())
    # The right answer is escalate, but the model insists on a rollback with no earlier revision.
    insists = [[call("submit_diagnosis", GOOD)], [call("submit_diagnosis", GOOD)]]
    summary, rec_dir = record(tmp, insists, revisions=(2,), repeats=1, scenario=UNSCHEDULABLE)
    assert summary["action_accuracy"] == 1.0 and summary["model_action_accuracy"] == 0.0 and summary["overrides"] == 1
    replayed = replay(rec_dir, tmp, repeats=1, scenario=UNSCHEDULABLE)
    assert replayed["overrides"] == 1          # the guardrail's own cluster facts were taped too


def test_tapes_hold_only_redacted_data():
    _, rec_dir = record(Path(tempfile.mkdtemp()), [[call("get_deployment", {"name": "worker"})],
                                                   [call("get_logs", {"pod": "worker-abc"})],
                                                   [call("submit_diagnosis", GOOD)]], repeats=1)
    tape = (rec_dir / "tape.json").read_text()
    assert "hunter2" not in tape and "[REDACTED]" in tape     # fake cluster's password env var and log line


def test_changed_prompt_or_extra_calls_need_a_rerecord():
    tmp = Path(tempfile.mkdtemp())
    _, rec_dir = record(tmp, SCRIPT * 2)
    tape = TapePlayer(rec_dir)
    with pytest.raises(NotRecorded, match="make record-agent"):
        tape.model().converse_tools("a different system prompt", [], [])


def test_cli_replays_an_agent_recording_and_applies_the_gate():
    tmp = Path(tempfile.mkdtemp())
    cited = GOOD | {"citations": ["oomkilled"]}
    turns = [[call("list_pods")], [call("read_runbook", {"id": "oomkilled"})], [call("submit_diagnosis", cited)]]
    _, rec_dir = record(tmp, turns * 2, rag=True)       # the gate needs citations measured, so use runbooks
    saved, run_module.REPORTS_DIR = run_module.REPORTS_DIR, tmp / "reports"
    try:
        assert run_module.main(["--replay", str(rec_dir), "--gate", str(gate.GATE_FILE), "--scenario", "oom-killed"]) == 0
    finally:
        run_module.REPORTS_DIR = saved
