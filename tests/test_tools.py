"""Checks the agent's tools stay in their namespace, reject injected or malformed arguments, never return secret or ConfigMap values, and log every call."""
import json

from kubemedic.redact import MASK
from kubemedic.tools import MAX_RESULT_CHARS, SPECS, Toolbox

NS = "km-oom-killed"
DEPLOYMENT = {
    "metadata": {"name": "worker", "uid": "u1", "annotations": {"deployment.kubernetes.io/revision": "2"}},
    "spec": {"replicas": 1, "template": {"spec": {"containers": [{
        "name": "worker", "image": "busybox:1.36",
        "env": [{"name": "CACHE_MB", "value": "200"}, {"name": "DB_PASSWORD", "value": "hunter2hunter2"}]}]}}},
    "status": {"replicas": 1, "readyReplicas": 0,
               "conditions": [{"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"},
                              {"type": "Progressing", "status": "True"}]},
}
POD = {"metadata": {"name": "worker-abc", "ownerReferences": [{"name": "worker-5d"}]},
       "spec": {"containers": [{"name": "worker"}]},
       "status": {"phase": "Running", "containerStatuses": [{
           "name": "worker", "ready": False, "restartCount": 3, "state": {"waiting": {"reason": "CrashLoopBackOff"}},
           "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}


class FakeKubectl:
    """Answers like `kubectl --as <agent>` would, and records every command it was asked to run."""

    def __init__(self, logs="starting\npassword=hunter2hunter2\nkilled", objects=None):
        self.calls, self.logs = [], logs
        self.objects = objects or {
            ("deployments",): {"items": [DEPLOYMENT]}, ("replicasets",): {"items": []},
            ("pods",): {"items": [POD]},
            ("events",): {"items": [{"type": "Warning", "reason": "BackOff", "message": "Back-off restarting",
                                     "involvedObject": {"kind": "Pod", "name": "worker-abc"}, "metadata": {}},
                                    {"type": "Normal", "reason": "Scheduled", "message": "ok",
                                     "involvedObject": {"kind": "Pod", "name": "other"}, "metadata": {}}]},
            ("deployment", "worker"): DEPLOYMENT,
            ("configmap", "worker-config"): {"metadata": {"name": "worker-config"}, "data": {"settings.yaml": "s3cret: x"}},
        }

    def get(self, ns, *what):
        self.calls.append(("get", ns, *what))
        return self.objects.get(what)

    def run(self, *args):
        self.calls.append(("run", *args))
        return True, self.logs


def toolbox(**kw):
    return Toolbox(NS, FakeKubectl(**kw))


def test_every_kubectl_call_is_pinned_to_the_namespace():
    tb = toolbox()
    for name, args in [("list_deployments", {}), ("get_deployment", {"name": "worker"}), ("list_pods", {}),
                       ("get_events", {}), ("get_logs", {"pod": "worker-abc"}),
                       ("get_configmap_keys", {"name": "worker-config"})]:
        assert "error" not in tb.call(name, args), name
    for call in tb.k.calls:
        assert (call[1] == NS) if call[0] == "get" else ("-n" in call and call[call.index("-n") + 1] == NS)


def test_no_tool_accepts_a_namespace_argument():
    assert all("namespace" not in spec["schema"].get("properties", {}) for spec in SPECS.values())
    tb = toolbox()
    assert "error" in tb.call("list_pods", {"namespace": "kube-system"})
    assert tb.k.calls == []                     # rejected before kubectl ran


def test_flag_injection_in_names_is_blocked():
    tb = toolbox()
    for bad in ["--all-namespaces", "-n", "worker -n kube-system", "../etc", "Worker", ""]:
        assert "invalid arguments" in tb.call("get_deployment", {"name": bad})["error"], bad
        assert "invalid arguments" in tb.call("get_logs", {"pod": bad})["error"], bad
    assert tb.k.calls == []


def test_unknown_tool_and_missing_args_come_back_as_errors_not_crashes():
    tb = toolbox()
    assert "unknown tool" in tb.call("delete_pod", {"name": "x"})["error"]
    assert "invalid arguments" in tb.call("get_deployment", {})["error"]
    assert "not found" in tb.call("get_deployment", {"name": "nope"})["error"]
    assert [e["ok"] for e in tb.log] == [False, False, False]


def test_secret_and_configmap_values_never_come_back():
    tb = toolbox()
    results = json.dumps([tb.call("get_deployment", {"name": "worker"}), tb.call("get_logs", {"pod": "worker-abc"}),
                          tb.call("get_configmap_keys", {"name": "worker-config"})])
    assert "hunter2" not in results and "s3cret" not in results and MASK in results
    assert tb.call("get_configmap_keys", {"name": "worker-config"})["keys"] == ["settings.yaml"]


def test_logs_previous_and_container_flags():
    tb = toolbox()
    tb.call("get_logs", {"pod": "worker-abc", "container": "worker", "previous": True})
    assert tb.k.calls[-1][-3:] == ("-c", "worker", "--previous")


def test_events_can_be_filtered_by_object():
    events = toolbox().call("get_events", {"object": "worker-abc"})["events"]
    assert [e["reason"] for e in events] == ["BackOff"]


def test_list_deployments_shows_only_unhealthy_conditions():
    d = toolbox().call("list_deployments", {})["deployments"][0]
    assert d["name"] == "worker" and [c["type"] for c in d["unhealthy_conditions"]] == ["Available"]


def test_big_results_are_capped():
    tb = toolbox(logs="x" * 50_000)
    tb.call("list_pods", {})
    tb.k.objects[("pods",)] = {"items": [POD] * 200}
    result = tb.call("list_pods", {})
    assert result["truncated_to_chars"] == MAX_RESULT_CHARS and len(result["partial_json"]) == MAX_RESULT_CHARS


def test_runbook_tools_only_exist_with_a_retriever_and_track_pages_read():
    assert "search_runbooks" not in toolbox().names

    class FakeRetriever:
        from kubemedic.runbooks import load_all
        runbooks = {rb.id: rb for rb in load_all()}

    tb = Toolbox(NS, FakeKubectl(), FakeRetriever())
    assert "read_runbook" in tb.names and len(tb.bedrock_specs()) == len(SPECS)
    assert tb.call("read_runbook", {"id": "oomkilled"})["id"] == "oomkilled"
    assert "error" in tb.call("read_runbook", {"id": "made-up-page"})
    assert tb.runbooks_read == ["oomkilled"]


def test_bedrock_spec_shape():
    spec = toolbox().bedrock_specs()[0]["toolSpec"]
    assert set(spec) == {"name", "description", "inputSchema"} and spec["inputSchema"]["json"]["type"] == "object"
