"""Checks the agent loop runs tools and sends results back, accepts a good diagnosis, enforces citations and guardrails with facts it fetches itself, and stops at its budgets."""
import json

from kubemedic.agent import SUBMIT, SYSTEM_PROMPT_AGENT, TASK, deployment_facts, investigate
from kubemedic.runbooks import load_all
from kubemedic.tools import Toolbox
from test_tools import DEPLOYMENT, NS, FakeKubectl


def rs(revision):
    return {"metadata": {"annotations": {"deployment.kubernetes.io/revision": str(revision)},
                         "ownerReferences": [{"uid": "u1"}]},
            "spec": {"template": {"spec": {"containers": [{"name": "worker", "image": "busybox:1.36"}]}}}}


def kubectl(revisions=(1, 2)):
    k = FakeKubectl()
    k.objects[("replicasets",)] = {"items": [rs(r) for r in revisions]}
    return k


def call(name, args=None, n=[0]):
    n[0] += 1
    return {"toolUse": {"toolUseId": f"t{n[0]}", "name": name, "input": args or {}}}


def diagnosis(action="rollback", target="Deployment/worker", citations=None):
    d = {"root_cause": "CACHE_MB=200 pushes worker past its memory limit", "evidence": ["OOMKilled"],
         "proposed_action": {"action": action, "target": target, "reason": "r"}, "confidence": "high"}
    if action == "escalate":
        d["proposed_action"].pop("target")
    if citations is not None:
        d["citations"] = citations
    return d


class ScriptedAgentModel:
    """Plays back a list of replies (each a list of content blocks) and records every request."""
    model_id = "fake-model"

    def __init__(self, *turns):
        self.turns, self.requests = list(turns), []

    def converse_tools(self, system, messages, tools):
        self.requests.append({"system": system, "messages": json.loads(json.dumps(messages)), "tools": tools})
        turn = self.turns.pop(0) if self.turns else [call("list_pods")]
        return turn, {"input_tokens": 1000, "output_tokens": 50}


def test_investigates_then_submits_a_valid_diagnosis():
    model = ScriptedAgentModel(
        [{"text": "<thinking>start with deployments</thinking>"}, call("list_deployments")],
        [call("get_deployment", {"name": "worker"}), call("list_pods")],
        [call(SUBMIT, diagnosis())])
    r = investigate(Toolbox(NS, kubectl()), model)
    assert r.valid and r.diagnosis["proposed_action"]["action"] == "rollback"
    assert r.steps == 3 and [c["tool"] for c in r.tool_calls] == ["list_deployments", "get_deployment", "list_pods"]
    assert r.input_tokens == 3000 and r.prompt_version == "a1"
    assert "start with deployments" in r.trace[0]["thinking"]


def test_tool_results_go_back_with_matching_ids_and_errors_flagged():
    model = ScriptedAgentModel([call("list_pods"), call("get_deployment", {"name": "--all-namespaces"})],
                               [call(SUBMIT, diagnosis())])
    investigate(Toolbox(NS, kubectl()), model)
    sent = model.requests[1]["messages"][-1]["content"]
    asked = [b["toolUse"]["toolUseId"] for b in model.requests[1]["messages"][-2]["content"]]
    assert [b["toolResult"]["toolUseId"] for b in sent] == asked
    assert [b["toolResult"]["status"] for b in sent] == ["success", "error"]


def test_the_model_is_never_told_the_namespace():
    model = ScriptedAgentModel([call(SUBMIT, diagnosis())])
    investigate(Toolbox(NS, kubectl()), model)
    request = json.dumps(model.requests[0])
    assert NS not in request and "oom" not in request.lower().replace("room", "")
    assert TASK in request and "untrusted" in SYSTEM_PROMPT_AGENT


def test_guardrail_uses_cluster_facts_and_fails_closed():
    # Only the current revision (2) exists, so a rollback is impossible. The model insists twice.
    model = ScriptedAgentModel([call(SUBMIT, diagnosis())], [call(SUBMIT, diagnosis())])
    r = investigate(Toolbox(NS, kubectl(revisions=(2,))), model)
    assert r.valid and r.overridden and r.diagnosis["proposed_action"]["action"] == "escalate"
    assert "rollback needs an earlier revision" in r.guardrail_violations[0]
    rejection = model.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert rejection["status"] == "error" and "diagnosis rejected" in rejection["content"][0]["json"]["error"]


def test_the_model_can_correct_itself_after_a_rejection():
    model = ScriptedAgentModel([call(SUBMIT, diagnosis())], [call(SUBMIT, diagnosis(action="escalate"))])
    r = investigate(Toolbox(NS, kubectl(revisions=(2,))), model)
    assert r.valid and not r.overridden and r.attempts == 2 and r.guardrail_violations


def test_injected_target_never_reaches_kubectl():
    k = kubectl()
    facts = deployment_facts(Toolbox(NS, k), diagnosis(target="Deployment/--all-namespaces"))
    assert facts == {"deployments": []} and k.calls == []


def test_step_budget_stops_a_model_that_never_finishes():
    r = investigate(Toolbox(NS, kubectl()), ScriptedAgentModel(), max_steps=4)
    assert not r.valid and r.steps == 4 and "step budget exhausted" in r.problems[-1]


def test_a_chatty_reply_is_nudged_back_to_tools():
    model = ScriptedAgentModel([{"text": "I think it is memory."}], [call(SUBMIT, diagnosis())])
    r = investigate(Toolbox(NS, kubectl()), model)
    assert r.valid and "Use a tool" in model.requests[1]["messages"][-1]["content"][0]["text"]


def test_with_runbooks_it_may_only_cite_pages_it_read():
    class Retriever:
        runbooks = {rb.id: rb for rb in load_all()}

    model = ScriptedAgentModel([call(SUBMIT, diagnosis(citations=["oomkilled"]))],     # cites without reading
                               [call("read_runbook", {"id": "oomkilled"})],
                               [call(SUBMIT, diagnosis(citations=["oomkilled"]))])
    r = investigate(Toolbox(NS, kubectl(), Retriever()), model)
    assert r.valid and r.prompt_version == "a1-rag" and r.diagnosis["citations"] == ["oomkilled"]
    first_rejection = model.requests[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["json"]["error"]
    assert "runbooks you have read: none" in first_rejection
    assert any(t["toolSpec"]["name"] == "read_runbook" for t in model.requests[0]["tools"])


def test_submit_tool_schema_is_nova_compatible():
    model = ScriptedAgentModel([call(SUBMIT, diagnosis())])
    investigate(Toolbox(NS, kubectl()), model)
    submit = next(t for t in model.requests[0]["tools"] if t["toolSpec"]["name"] == SUBMIT)["toolSpec"]
    assert set(submit["inputSchema"]["json"]) == {"type", "properties", "required"}
