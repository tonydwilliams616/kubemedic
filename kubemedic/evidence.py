"""Gathers a compact, redacted evidence bundle for one namespace, using only the agent's read-only identity.

Two layers:
- Summarisers (pure functions) turn raw `kubectl get -o json` output into small dicts that
  keep only what helps a diagnosis. They are unit-tested without a cluster.
- Gatherers run kubectl *as the agent's ServiceAccount* (impersonation), so even when you
  run this from your admin kubeconfig, it can only see what the agent's RBAC allows.

Usage: python -m kubemedic.evidence --namespace km-oom-killed [--context kind-kubemedic]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

from .redact import cap, redact, redact_value

AGENT_USER = "system:serviceaccount:kubemedic-system:kubemedic-agent"
MAX_MESSAGE = 300      # chars kept from any single status/event message
MAX_LOG = 2000         # chars kept per log (the END of the log)
LOG_TAIL_LINES = 50
MAX_EVENTS = 20


def _msg(text: str | None) -> str | None:
    """Redact and shorten a status or event message."""
    if not text:
        return None
    return cap(redact(text)[0], MAX_MESSAGE)


# ---------- summarisers: raw kubectl JSON -> small dicts ----------

def summarise_pod(pod: dict) -> dict:
    status = pod.get("status", {})
    statuses = status.get("containerStatuses", [])
    containers = []
    for cs in statuses:
        entry = {"name": cs["name"], "image": cs.get("image"), "ready": cs.get("ready", False),
                 "restarts": cs.get("restartCount", 0)}
        for key in ("waiting", "terminated"):
            st = cs.get("state", {}).get(key)
            if st:
                entry["state"] = {key: st.get("reason"), "message": _msg(st.get("message")),
                                  **({"exit_code": st["exitCode"]} if "exitCode" in st else {})}
        if "running" in cs.get("state", {}):
            entry["state"] = {"running": True}
        last = cs.get("lastState", {}).get("terminated")
        if last:
            entry["last_terminated"] = {"reason": last.get("reason"), "exit_code": last.get("exitCode")}
        containers.append(entry)
    owners = pod["metadata"].get("ownerReferences", [])
    return {
        "name": pod["metadata"]["name"],
        "owner": owners[0]["name"] if owners else None,
        "phase": status.get("phase"),
        "ready": f"{sum(c['ready'] for c in containers)}/{len(pod['spec']['containers'])}",
        "containers": containers,
        # Only conditions that are NOT healthy are worth the model's attention.
        "problem_conditions": [
            {"type": c["type"], "reason": c.get("reason"), "message": _msg(c.get("message"))}
            for c in status.get("conditions", []) if c.get("status") != "True"
        ],
    }


def summarise_events(events: list[dict], limit: int = MAX_EVENTS) -> list[dict]:
    def when(e: dict) -> str:
        return e.get("lastTimestamp") or e.get("eventTime") or e["metadata"].get("creationTimestamp") or ""

    out = []
    for e in sorted(events, key=when)[-limit:]:
        obj = e.get("involvedObject", {})
        out.append({"type": e.get("type"), "reason": e.get("reason"),
                    "object": f"{obj.get('kind')}/{obj.get('name')}",
                    "count": e.get("count", 1), "message": _msg(e.get("message"))})
    return out


def _env_entry(env: dict) -> dict:
    if "value" in env:
        return {"name": env["name"], "value": redact_value(env["name"], env["value"])}
    src = env.get("valueFrom", {})
    for kind in ("secretKeyRef", "configMapKeyRef"):
        if kind in src:
            # Show WHERE the value comes from, never the value itself.
            return {"name": env["name"], "from": {kind: {"name": src[kind].get("name"), "key": src[kind].get("key")}}}
    return {"name": env["name"], "from": "other"}


def _probe(probe: dict | None) -> dict | None:
    if not probe:
        return None
    if "httpGet" in probe:
        return {"httpGet": f"{probe['httpGet'].get('path', '/')}:{probe['httpGet'].get('port')}"}
    if "tcpSocket" in probe:
        return {"tcpSocket": probe["tcpSocket"].get("port")}
    if "exec" in probe:
        return {"exec": True}
    return {"other": True}


def summarise_deployment(dep: dict, replicasets: list[dict]) -> dict:
    spec, status = dep["spec"], dep.get("status", {})
    tmpl = spec["template"]["spec"]
    containers = [{
        "name": c["name"], "image": c.get("image"),
        "env": [_env_entry(e) for e in c.get("env", [])],
        "resources": c.get("resources", {}),
        "readiness_probe": _probe(c.get("readinessProbe")),
        "liveness_probe": _probe(c.get("livenessProbe")),
    } for c in tmpl.get("containers", [])]
    return {
        "name": dep["metadata"]["name"],
        "replicas": {k: status.get(k, 0) for k in ("replicas", "readyReplicas", "updatedReplicas", "availableReplicas")}
                    | {"desired": spec.get("replicas", 1)},
        "conditions": [{"type": c["type"], "status": c["status"], "reason": c.get("reason"),
                        "message": _msg(c.get("message"))} for c in status.get("conditions", [])],
        "containers": containers,
        "configmaps_used": sorted({v["configMap"]["name"] for v in tmpl.get("volumes", []) if "configMap" in v}),
        "rollout_history": rollout_history(dep, replicasets),
    }


def _container_view(c: dict) -> dict:
    """The parts of a container spec that releases usually change, with secret values hidden."""
    return {
        "image": c.get("image"),
        "env": {e["name"]: _env_entry(e).get("value", _env_entry(e).get("from")) for e in c.get("env", [])},
        "readiness_probe": _probe(c.get("readinessProbe")),
        "liveness_probe": _probe(c.get("livenessProbe")),
        "resources": c.get("resources", {}),
    }


def template_changes(old: dict, new: dict) -> list[str]:
    """Human-readable differences between two pod templates: what the release actually changed."""
    changes = []
    old_c = {c["name"]: _container_view(c) for c in old.get("spec", {}).get("containers", [])}
    new_c = {c["name"]: _container_view(c) for c in new.get("spec", {}).get("containers", [])}
    for name in sorted(old_c.keys() | new_c.keys()):
        if name not in old_c or name not in new_c:
            changes.append(f"container {name}: {'added' if name in new_c else 'removed'}")
            continue
        o, n = old_c[name], new_c[name]
        for field in ("image", "readiness_probe", "liveness_probe", "resources"):
            if o[field] != n[field]:
                changes.append(f"container {name}: {field} {json.dumps(o[field])} -> {json.dumps(n[field])}")
        for var in sorted(o["env"].keys() | n["env"].keys()):
            if o["env"].get(var) != n["env"].get(var):
                changes.append(f"container {name}: env {var} {json.dumps(o['env'].get(var))} -> {json.dumps(n['env'].get(var))}")
    return changes


def rollout_history(dep: dict, replicasets: list[dict]) -> list[dict]:
    """Revisions oldest -> newest: which one is current, and what each changed versus the one before.

    Deliberately no per-revision ready counts: an old revision has 0 ready pods simply because it
    was scaled down, which says nothing about whether it was healthy. That misled the model in v1.
    """
    uid = dep["metadata"].get("uid")
    current = dep["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision")
    owned = [rs for rs in replicasets
             if any(o.get("uid") == uid for o in rs["metadata"].get("ownerReferences", []))]
    owned.sort(key=lambda rs: int(rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", 0)))
    history, previous = [], None
    for rs in owned:
        ann = rs["metadata"].get("annotations", {})
        revision = ann.get("deployment.kubernetes.io/revision", "0")
        tmpl = rs["spec"]["template"]
        history.append({
            "revision": int(revision),
            "current": revision == current,
            "change_cause": ann.get("kubernetes.io/change-cause"),
            "images": [c.get("image") for c in tmpl["spec"].get("containers", [])],
            "changes_from_previous": template_changes(previous, tmpl) if previous else None,
        })
        previous = tmpl
    return history


def configmap_keys(cm: dict) -> dict:
    """Key NAMES only. Values can hold sensitive config and are never returned."""
    return {"name": cm["metadata"]["name"],
            "keys": sorted(list(cm.get("data", {})) + list(cm.get("binaryData", {})))}


# ---------- gatherers: run kubectl as the agent ----------

class AgentKubectl:
    def __init__(self, context: str):
        self.context = context

    def run(self, *args: str) -> tuple[bool, str]:
        cmd = ["kubectl", "--context", self.context, "--as", AGENT_USER, *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.returncode == 0, proc.stdout if proc.returncode == 0 else proc.stderr.strip()

    def get(self, ns: str, *what: str) -> dict | None:
        ok, out = self.run("get", *what, "-n", ns, "-o", "json")
        return json.loads(out) if ok else None


def gather(ns: str, context: str = "kind-kubemedic", k: AgentKubectl | None = None) -> dict:
    k = k or AgentKubectl(context)  # tests pass a fake here, so no cluster is needed
    errors: list[str] = []

    def get_items(kind: str) -> list[dict]:
        data = k.get(ns, kind)
        if data is None:
            errors.append(f"could not list {kind} in {ns}")
            return []
        return data["items"]

    pods = get_items("pods")
    deployments = get_items("deployments")
    replicasets = get_items("replicasets")
    events = get_items("events")

    configmaps = []
    for name in sorted({n for d in deployments for n in summarise_deployment(d, []).get("configmaps_used", [])}):
        cm = k.get(ns, "configmap", name)
        configmaps.append(configmap_keys(cm) if cm else {"name": name, "keys": None, "error": "not found or not readable"})

    logs = []
    for p in pods:
        for cs in p.get("status", {}).get("containerStatuses", []):
            if cs.get("ready"):
                continue  # healthy containers rarely explain a fault; keep the bundle small
            for previous in ([False, True] if cs.get("restartCount", 0) > 0 else [False]):
                args = ["logs", p["metadata"]["name"], "-c", cs["name"], "-n", ns, f"--tail={LOG_TAIL_LINES}"]
                if previous:
                    args.append("--previous")
                ok, out = k.run(*args)
                text, n = redact(out)
                logs.append({"pod": p["metadata"]["name"], "container": cs["name"], "previous": previous,
                             "ok": ok, "redactions": n, "text": cap(text, MAX_LOG)})

    return {
        "namespace": ns,
        "deployments": [summarise_deployment(d, replicasets) for d in deployments],
        "pods": [summarise_pod(p) for p in pods],
        "events": summarise_events(events),
        "configmaps": configmaps,
        "logs": logs,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Print the agent's evidence bundle for a namespace")
    p.add_argument("--namespace", "-n", required=True)
    p.add_argument("--context", default="kind-kubemedic")
    args = p.parse_args(argv)
    bundle = gather(args.namespace, args.context)
    text = json.dumps(bundle, indent=2)
    print(text)
    print(f"\n# bundle size: {len(text)} chars (~{len(text) // 4} tokens)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
