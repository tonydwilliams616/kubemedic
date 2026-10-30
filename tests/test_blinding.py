"""Checks lab namespace names (which name the fault) never reach the model through the evidence bundle, the one-shot prompt or agent tool results, while real namespaces are left alone."""
import json

from kubemedic.agent import investigate
from kubemedic.diagnose import build_user_message
from kubemedic.evidence import gather
from kubemedic.redact import NAMESPACE_MASK, blind_namespace
from kubemedic.tools import Toolbox
from test_agent import ScriptedAgentModel, call, diagnosis, kubectl

NS = "km-oom-killed"
# A real kubelet event message: it names the pod as <pod>_<namespace>(<uid>).
LEAKY_EVENT = {"type": "Warning", "reason": "BackOff", "metadata": {},
               "involvedObject": {"kind": "Pod", "name": "worker-abc"},
               "message": f"Back-off restarting failed container worker in pod worker-abc_{NS}(9ffc8a4e)"}


def test_lab_namespaces_are_blinded_everywhere_inside_nested_data():
    data = {"a": [f"pod x_{NS}(uid)", {"b": NS}], "n": 3}
    assert blind_namespace(data, NS) == {"a": [f"pod x_{NAMESPACE_MASK}(uid)", {"b": NAMESPACE_MASK}], "n": 3}


def test_real_namespaces_are_left_alone():
    # In a real cluster the namespace may equal a Deployment name; blinding would destroy the target.
    data = {"target": "Deployment/payments", "msg": "pod payments-1_payments(uid)"}
    assert blind_namespace(data, "payments") == data


class FakeKubectl:
    def get(self, ns, *what):
        return {"events": {"items": [LEAKY_EVENT]}}.get(what[0], {"items": []})

    def run(self, *args):
        return True, ""


def test_gather_keeps_the_name_for_code_but_hides_it_in_everything_else():
    bundle = gather(NS, k=FakeKubectl())
    assert bundle["namespace"] == NS
    rest = json.dumps({k: v for k, v in bundle.items() if k != "namespace"})
    assert NS not in rest and NAMESPACE_MASK in rest


def test_one_shot_prompt_never_contains_the_namespace():
    leaky = {"namespace": NS, "events": [{"message": f"pod worker-abc_{NS}(uid)"}]}   # e.g. an old, unblinded bundle
    assert NS not in build_user_message(leaky)


def test_agent_tool_results_never_contain_the_namespace():
    k = kubectl()
    k.objects[("events",)] = {"items": [LEAKY_EVENT]}
    model = ScriptedAgentModel([call("get_events")], [call("submit_diagnosis", diagnosis())])
    investigate(Toolbox(NS, k), model)
    assert NS not in json.dumps(model.requests[-1]) and NAMESPACE_MASK in json.dumps(model.requests[-1])
