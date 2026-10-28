"""Checks the evidence bundle keeps what a diagnosis needs, and never leaks secret or ConfigMap values."""
import json

from kubemedic.evidence import (
    AGENT_USER, configmap_keys, gather, rollout_history, summarise_deployment,
    summarise_events, summarise_pod,
)
from kubemedic.redact import MASK


def pod(name="api-1", waiting=None, last_terminated=None, restarts=0, ready=False, conditions=()):
    state = {"waiting": {"reason": waiting, "message": "back-off"}} if waiting else {"running": {}}
    cs = {"name": "api", "image": "busybox:1.36", "ready": ready, "restartCount": restarts, "state": state}
    if last_terminated:
        cs["lastState"] = {"terminated": {"reason": last_terminated, "exitCode": 137}}
    return {
        "metadata": {"name": name, "ownerReferences": [{"name": "api-6bb856d98c"}]},
        "spec": {"containers": [{"name": "api"}]},
        "status": {"phase": "Running", "containerStatuses": [cs], "conditions": list(conditions)},
    }


def deployment():
    return {
        "metadata": {"name": "api", "uid": "dep-uid"},
        "spec": {"replicas": 1, "template": {"spec": {
            "containers": [{
                "name": "api", "image": "busybox:1.36",
                "env": [
                    {"name": "CONFIG_PATH", "value": "/etc/app/settings.yaml"},
                    {"name": "DB_PASSWORD", "value": "hunter2hunter2"},
                    {"name": "API_KEY", "valueFrom": {"secretKeyRef": {"name": "api-secrets", "key": "key"}}},
                ],
                "readinessProbe": {"httpGet": {"path": "/healthz", "port": 80}},
            }],
            "volumes": [{"name": "config", "configMap": {"name": "api-config"}}],
        }}},
        "status": {"replicas": 1, "readyReplicas": 0,
                   "conditions": [{"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"}]},
    }


def replicaset(revision, image, cause, owner="dep-uid"):
    return {
        "metadata": {"annotations": {"deployment.kubernetes.io/revision": str(revision),
                                     "kubernetes.io/change-cause": cause},
                     "ownerReferences": [{"uid": owner}]},
        "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
        "status": {"readyReplicas": 1 if revision == 1 else 0},
    }


# ---------- summarisers ----------

def test_pod_summary_shows_waiting_reason_and_last_crash():
    s = summarise_pod(pod(waiting="CrashLoopBackOff", last_terminated="OOMKilled", restarts=4))
    c = s["containers"][0]
    assert c["state"]["waiting"] == "CrashLoopBackOff"
    assert c["last_terminated"] == {"reason": "OOMKilled", "exit_code": 137}
    assert c["restarts"] == 4 and s["ready"] == "0/1" and s["owner"] == "api-6bb856d98c"


def test_pod_summary_keeps_only_unhealthy_conditions():
    s = summarise_pod(pod(conditions=[{"type": "Ready", "status": "False", "reason": "ContainersNotReady"},
                                      {"type": "PodScheduled", "status": "True"}]))
    assert [c["type"] for c in s["problem_conditions"]] == ["Ready"]


def test_deployment_env_hides_secret_values_but_keeps_useful_ones():
    env = {e["name"]: e for e in summarise_deployment(deployment(), [])["containers"][0]["env"]}
    assert env["CONFIG_PATH"]["value"] == "/etc/app/settings.yaml"
    assert env["DB_PASSWORD"]["value"] == MASK
    assert env["API_KEY"] == {"name": "API_KEY", "from": {"secretKeyRef": {"name": "api-secrets", "key": "key"}}}


def test_deployment_summary_includes_probe_and_configmaps():
    d = summarise_deployment(deployment(), [])
    assert d["containers"][0]["readiness_probe"] == {"httpGet": "/healthz:80"}
    assert d["configmaps_used"] == ["api-config"]


def test_rollout_history_is_ordered_and_ignores_other_deployments():
    rss = [replicaset(2, "nginx:1.27.99-alpine", "release 1.5.0"),
           replicaset(1, "nginx:1.27-alpine", "release 1.4.0"),
           replicaset(7, "other:1", "unrelated", owner="someone-else")]
    h = rollout_history(deployment(), rss)
    assert [r["revision"] for r in h] == [1, 2]
    assert h[-1]["images"] == ["nginx:1.27.99-alpine"] and h[-1]["change_cause"] == "release 1.5.0"


def test_configmap_returns_key_names_never_values():
    out = configmap_keys({"metadata": {"name": "api-config"}, "data": {"config.yaml": "password: hunter2"}})
    assert out == {"name": "api-config", "keys": ["config.yaml"]}
    assert "hunter2" not in json.dumps(out)


def test_events_are_newest_last_capped_and_redacted():
    evs = [{"metadata": {}, "lastTimestamp": f"2026-10-28T08:00:{i:02d}Z", "type": "Warning", "reason": f"R{i}",
            "involvedObject": {"kind": "Pod", "name": "p"}, "message": "token=abcdef123456"} for i in range(30)]
    out = summarise_events(evs, limit=5)
    assert [e["reason"] for e in out] == ["R25", "R26", "R27", "R28", "R29"]
    assert "abcdef123456" not in out[0]["message"]


# ---------- gather, with a fake kubectl ----------

class FakeKubectl:
    """Answers like kubectl would for a crash-looping 'api' deployment; records every call."""

    def __init__(self):
        self.calls = []

    def run(self, *args):
        self.calls.append(args)
        if args[0] == "logs":
            return True, "FATAL: config file /etc/app/settings.yaml not found\nDB_PASSWORD=hunter2hunter2"
        return False, "not used"

    def get(self, ns, *what):
        self.calls.append(("get", *what))
        items = {
            ("pods",): [pod(waiting="CrashLoopBackOff", restarts=3)],
            ("deployments",): [deployment()],
            ("replicasets",): [replicaset(1, "busybox:1.36", "release 2.3.0")],
            ("events",): [],
        }
        if what == ("configmap", "api-config"):
            return {"metadata": {"name": "api-config"}, "data": {"config.yaml": "port: 8080"}}
        return {"items": items.get(what, [])}


def test_gather_builds_a_bundle_with_current_and_previous_logs():
    fake = FakeKubectl()
    b = gather("km-test", k=fake)
    assert b["configmaps"] == [{"name": "api-config", "keys": ["config.yaml"]}]
    assert [l["previous"] for l in b["logs"]] == [False, True]   # restarted, so both logs are fetched
    assert "settings.yaml not found" in b["logs"][0]["text"]
    assert b["errors"] == []


def test_gather_output_contains_no_secret_values_anywhere():
    text = json.dumps(gather("km-test", k=FakeKubectl()))
    assert "hunter2hunter2" not in text
    assert "port: 8080" not in text   # ConfigMap values never appear


def test_gather_reports_forbidden_lists_instead_of_crashing():
    class Denied(FakeKubectl):
        def get(self, ns, *what):
            return None if what == ("pods",) else super().get(ns, *what)
    b = gather("km-test", k=Denied())
    assert b["pods"] == [] and "could not list pods in km-test" in b["errors"]


def test_agent_identity_is_the_read_only_service_account():
    assert AGENT_USER == "system:serviceaccount:kubemedic-system:kubemedic-agent"
