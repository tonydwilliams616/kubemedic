"""Checks a lab break always starts from a clean namespace and grants both identities, the agent's and the executor's, inside that namespace only."""
import json
from types import SimpleNamespace

import lab.__main__ as lab_main


class RecordingKubectl:
    def __init__(self, context):
        self.calls = []
        RecordingKubectl.last = self

    def run(self, *args, stdin=None):
        self.calls.append((args, json.loads(stdin) if stdin else None))
        return ""

    def apply_namespace(self, name, labels):
        self.calls.append((("apply-namespace", name), labels))


def test_break_resets_the_namespace_first_then_binds_both_identities():
    saved = lab_main.Kubectl
    lab_main.Kubectl = RecordingKubectl
    try:
        lab_main.cmd_break(SimpleNamespace(context="kind-kubemedic", all=False, scenario="oom-killed"))
    finally:
        lab_main.Kubectl = saved
    calls = RecordingKubectl.last.calls
    assert calls[0][0] == ("delete", "namespace", "km-oom-killed", "--ignore-not-found", "--wait=true")
    assert calls[1][0] == ("apply-namespace", "km-oom-killed")
    bindings = [body for _, body in calls if body and body.get("kind") == "RoleBinding"]
    assert [b["subjects"][0]["name"] for b in bindings] == ["kubemedic-agent", "kubemedic-executor"]
    assert all(b["metadata"]["namespace"] == "km-oom-killed" for b in bindings)
