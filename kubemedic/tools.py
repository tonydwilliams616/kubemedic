"""The agent's read-only tools: small, named questions it can ask about ONE namespace, each answered with redacted, size-capped data.

Why tools instead of one big evidence bundle: an engineer investigating an incident doesn't read
everything first. They list the pods, notice a crash, read that pod's logs, then check what the
last release changed. Tools let the model work the same way, one question at a time (the loop
that drives this is kubemedic/agent.py).

Safety matters more now, because the model chooses what runs:
- Read-only by construction. Every tool is a `kubectl get` or `kubectl logs` run as the agent's
  read-only ServiceAccount. No tool writes, execs, port-forwards or reads Secrets.
- The namespace is fixed when the Toolbox is created. No tool takes a namespace argument, so the
  model cannot look anywhere else, whatever it asks for.
- Arguments are checked against each tool's schema, and resource names must be valid Kubernetes
  names. That blocks argument injection: a "name" such as "--all-namespaces" would otherwise be
  read by kubectl as a flag.
- Every result is redacted and capped in size. Errors come back as data ({"error": ...}) so the
  model can try something else instead of the whole run crashing.
- Every call is logged (tool, arguments, outcome, size) for the audit trail and the evals.
"""
from __future__ import annotations

import json
import re

import jsonschema

from .evidence import (
    LOG_TAIL_LINES, MAX_LOG, AgentKubectl, configmap_keys, summarise_deployment, summarise_events, summarise_pod,
)
from .redact import blind_namespace, cap, redact

MAX_RESULT_CHARS = 6000          # per tool result: big enough for a deployment, small enough to keep costs down
# A Kubernetes object name (RFC 1123). It can't start with "-", so it can never become a kubectl flag.
NAME = {"type": "string", "pattern": r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$"}


class ToolError(Exception):
    """A problem the model should be told about, so it can try something else."""


# What the model is told about each tool. The descriptions matter: they are how the model decides
# which tool to use, so they say what comes back and when the tool is useful.
SPECS: dict[str, dict] = {
    "list_deployments": {
        "description": "List the Deployments in the namespace with desired and ready replicas and any unhealthy "
                       "conditions. Start here to see what is running and what is broken.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "get_deployment": {
        "description": "One Deployment in detail: containers (image, env with secret values hidden, resources, "
                       "probes), ConfigMaps it mounts, conditions, and rollout history showing which revision "
                       "is current and what each revision changed.",
        "schema": {"type": "object", "properties": {"name": NAME}, "required": ["name"],
                   "additionalProperties": False},
    },
    "list_pods": {
        "description": "List the pods with phase, readiness, restarts, container states (waiting/terminated "
                       "reasons and exit codes) and unhealthy conditions such as Unschedulable.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "get_events": {
        "description": "Recent events, newest last. Optionally only events whose object contains this text, "
                       "e.g. a pod or Deployment name.",
        "schema": {"type": "object", "properties": {"object": {"type": "string", "maxLength": 253}},
                   "additionalProperties": False},
    },
    "get_logs": {
        "description": "The last lines of a pod's logs, with secrets redacted. Set previous=true to read the "
                       "container instance that crashed before the current one (useful for CrashLoopBackOff).",
        "schema": {"type": "object",
                   "properties": {"pod": NAME, "container": NAME, "previous": {"type": "boolean"}},
                   "required": ["pod"], "additionalProperties": False},
    },
    "get_configmap_keys": {
        "description": "The key NAMES in a ConfigMap (never the values). Useful when an app can't find a config file.",
        "schema": {"type": "object", "properties": {"name": NAME}, "required": ["name"],
                   "additionalProperties": False},
    },
    "search_runbooks": {
        "description": "Search the team's runbooks by symptom (e.g. 'container OOMKilled exit code 137'). "
                       "Returns the 3 best matching page ids and titles; read one with read_runbook.",
        "schema": {"type": "object", "properties": {"query": {"type": "string", "minLength": 3, "maxLength": 500}},
                   "required": ["query"], "additionalProperties": False},
    },
    "read_runbook": {
        "description": "Read one runbook page by id: symptoms, likely causes, how to confirm, which mitigations apply.",
        "schema": {"type": "object", "properties": {"id": NAME}, "required": ["id"], "additionalProperties": False},
    },
}
RUNBOOK_TOOLS = {"search_runbooks", "read_runbook"}


def advertised(schema: dict) -> dict:
    """The simpler schema we SHOW the model; the strict one in SPECS is what we ENFORCE.

    Amazon Nova accepts only `type`, `properties` and `required` at the top level of a tool schema
    (`additionalProperties` makes it ignore the tool), so the strict parts are kept for our own
    validation in Toolbox.call, which is the check that actually protects the cluster.
    """
    keep = ("type", "description", "enum", "items", "properties", "required")
    out = {k: v for k, v in schema.items() if k in keep}
    if "properties" in out:
        out["properties"] = {name: advertised(p) for name, p in out["properties"].items()}
    if "items" in out:
        out["items"] = advertised(out["items"])
    return out


def bedrock_spec(name: str, description: str, schema: dict) -> dict:
    return {"toolSpec": {"name": name, "description": description, "inputSchema": {"json": advertised(schema)}}}


class Toolbox:
    """All the tools, pinned to one namespace. `call(name, args)` is the only way in."""

    def __init__(self, namespace: str, kubectl: AgentKubectl, retriever=None):
        self.ns, self.k, self.retriever = namespace, kubectl, retriever
        self.log: list[dict] = []             # every call, in order
        self.runbooks_read: list[str] = []    # pages actually read: the only ones the model may cite
        # Where questions actually get answered. Evals swap this for a recording or a replay
        # (evals/cassette.py), so everything around it (validation, blinding, caps, logging) still runs.
        self.backend = self._dispatch

    @property
    def names(self) -> list[str]:
        return [n for n in SPECS if self.retriever or n not in RUNBOOK_TOOLS]

    def bedrock_specs(self) -> list[dict]:
        """The tool list in the shape the Bedrock Converse API expects (toolConfig.tools)."""
        return [bedrock_spec(n, SPECS[n]["description"], SPECS[n]["schema"]) for n in self.names]

    def call(self, name: str, args: dict | None) -> dict:
        args = args or {}
        try:
            if name not in self.names:
                raise ToolError(f"unknown tool {name!r}; available: {', '.join(self.names)}")
            try:
                jsonschema.validate(args, SPECS[name]["schema"])
            except jsonschema.ValidationError as e:
                raise ToolError(f"invalid arguments for {name}: {e.message}") from None
            result = self.backend(name, args)
        except ToolError as e:
            result = {"error": str(e)}
        if name == "read_runbook" and "error" not in result and args["id"] not in self.runbooks_read:
            self.runbooks_read.append(args["id"])
        result = blind_namespace(result, self.ns)   # e.g. event messages name the pod as <pod>_<namespace>
        text = json.dumps(result)
        if len(text) > MAX_RESULT_CHARS:
            result = {"truncated_to_chars": MAX_RESULT_CHARS, "partial_json": text[:MAX_RESULT_CHARS]}
        self.log.append({"tool": name, "args": args, "ok": "error" not in result, "chars": min(len(text), MAX_RESULT_CHARS)})
        return result

    def deployment_facts(self, name: str) -> dict | None:
        """One Deployment's summary for the GUARDRAILS, not the model (it isn't in SPECS, so the model can't call it).

        Fetched through the same backend as the tools, so evals can record and replay it too.
        """
        if not re.fullmatch(NAME["pattern"], name):    # same injection rule as the tools
            return None
        out = self.backend("deployment_facts", {"name": name})
        return None if "error" in out else out

    def _dispatch(self, name: str, args: dict) -> dict:
        """Answer one question from the cluster or the runbooks. Problems come back as {"error": ...}."""
        try:
            return getattr(self, f"_{name}")(**args)
        except ToolError as e:
            return {"error": str(e)}

    # ---------- helpers ----------

    def _items(self, kind: str) -> list[dict]:
        data = self.k.get(self.ns, kind)
        if data is None:
            raise ToolError(f"could not list {kind} (not allowed or API error)")
        return data["items"]

    def _one(self, kind: str, name: str) -> dict:
        obj = self.k.get(self.ns, kind, name)
        if obj is None:
            raise ToolError(f"{kind} {name!r} not found in this namespace (or not allowed)")
        return obj

    # ---------- the tools (each matches an entry in SPECS) ----------

    def _list_deployments(self) -> dict:
        out = []
        for d in self._items("deployments"):
            s = summarise_deployment(d, [])
            out.append({"name": s["name"], "replicas": s["replicas"],
                        "unhealthy_conditions": [c for c in s["conditions"] if c["status"] != "True"]})
        return {"deployments": out}

    def _get_deployment(self, name: str) -> dict:
        return summarise_deployment(self._one("deployment", name), self._items("replicasets"))

    def _list_pods(self) -> dict:
        return {"pods": [summarise_pod(p) for p in self._items("pods")]}

    def _get_events(self, object: str | None = None) -> dict:  # noqa: A002 (the model-facing name reads best)
        events = summarise_events(self._items("events"), limit=1000)
        if object:
            events = [e for e in events if object in e["object"]]
        return {"events": events[-20:]}

    def _get_logs(self, pod: str, container: str | None = None, previous: bool = False) -> dict:
        args = ["logs", pod, "-n", self.ns, f"--tail={LOG_TAIL_LINES}"]
        if container:
            args += ["-c", container]
        if previous:
            args.append("--previous")
        ok, out = self.k.run(*args)
        if not ok:
            raise ToolError(f"could not read logs: {cap(redact(out)[0], 300)}")
        text, n = redact(out)
        return {"pod": pod, "previous": previous, "redactions": n, "text": cap(text, MAX_LOG)}

    def _get_configmap_keys(self, name: str) -> dict:
        return configmap_keys(self._one("configmap", name))

    def _search_runbooks(self, query: str) -> dict:
        from .retrieve import search
        hits = search(self.retriever.index, self.retriever.embedder, query, k=3)
        return {"results": [{"id": h["id"], "title": h["title"], "score": h["score"]} for h in hits]}

    def _read_runbook(self, id: str) -> dict:  # noqa: A002
        rb = self.retriever.runbooks.get(id)
        if rb is None:
            raise ToolError(f"no runbook with id {id!r}; use search_runbooks to find ids")
        return {"id": rb.id, "title": rb.title, "body": rb.body}

    def _deployment_facts(self, name: str) -> dict:
        return self._get_deployment(name)
