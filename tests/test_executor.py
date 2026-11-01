"""Checks the executor only plans allowlisted actions on valid targets, builds every command itself, re-checks preconditions on the live cluster, runs as its own identity, and only dry-runs."""
import pytest

from kubemedic.evidence import AGENT_USER
from kubemedic.executor import EXECUTOR_USER, Executor, ExecutorKubectl, Refused, plan
from test_agent import kubectl
from test_tools import NS


def proposal(action="rollback", target="Deployment/worker", reason="previous revision was healthy"):
    return {"action": action, "target": target, "reason": reason}


def test_rollback_and_restart_become_one_fixed_command_each():
    assert plan(proposal(), NS).command == ["rollout", "undo", "deployment/worker", "-n", NS]
    assert plan(proposal("rollout_restart"), NS).command == ["rollout", "restart", "deployment/worker", "-n", NS]


def test_escalate_plans_nothing():
    p = plan(proposal("escalate", target=None), NS)
    assert p.action == "escalate" and p.command == [] and p.deployment is None
    out = Executor(kubectl=kubectl()).dry_run(p)
    assert out.ok and not out.changed and "nothing changes" in out.summary


@pytest.mark.parametrize("bad", [
    proposal("scale"), proposal("delete"), proposal("kubectl_exec"),
    proposal(target="Pod/worker"), proposal(target="Deployment/--all-namespaces"),
    proposal(target="Deployment/worker -n kube-system"), proposal(target=None),
])
def test_anything_off_the_allowlist_or_malformed_is_refused(bad):
    with pytest.raises(Refused):
        plan(bad, NS)


def test_scale_is_refused_with_a_reason():
    with pytest.raises(Refused, match="no replica count"):
        plan(proposal("scale"), NS)


def test_a_bad_namespace_is_refused():
    with pytest.raises(Refused, match="namespace"):
        plan(proposal(), "--all-namespaces")


def test_the_proposal_reason_never_reaches_the_command():
    p = plan(proposal(reason="; kubectl delete ns --all"), NS)
    assert "delete" not in " ".join(p.command) and p.reason.startswith(";")


def test_dry_run_runs_server_side_as_the_executor_and_changes_nothing():
    k = kubectl(revisions=(1, 2))
    out = Executor(kubectl=k).dry_run(plan(proposal(), NS))
    assert out.ok and not out.changed and not out.refused
    assert k.calls[-1] == ("run", "rollout", "undo", "deployment/worker", "-n", NS, "--dry-run=server")
    assert "from revision 2" in out.summary and "to revision 1" in out.summary


def test_preconditions_are_rechecked_on_the_live_cluster():
    # Between proposal and approval the earlier revision disappeared: only revision 2 remains.
    out = Executor(kubectl=kubectl(revisions=(2,))).dry_run(plan(proposal(), NS))
    assert not out.ok and "earlier revision" in out.refused[0]
    missing = Executor(kubectl=kubectl()).dry_run(plan(proposal(target="Deployment/gone"), NS))
    assert not missing.ok and "not found" in missing.refused[0]


def test_a_dry_run_the_api_server_rejects_is_reported():
    k = kubectl(revisions=(1, 2))
    k.run = lambda *args: (False, "Error from server (Forbidden): cannot patch deployments")
    out = Executor(kubectl=k).dry_run(plan(proposal(), NS))
    assert not out.ok and "Forbidden" in out.refused[0]


def test_the_executor_has_its_own_identity():
    assert ExecutorKubectl.user == EXECUTOR_USER != AGENT_USER
    assert EXECUTOR_USER.endswith(":kubemedic-executor")


def test_the_lab_binds_the_executor_per_namespace_only():
    from lab.__main__ import agent_read_binding, executor_binding
    b = executor_binding("km-oom-killed")
    assert b["kind"] == "RoleBinding" and b["metadata"]["namespace"] == "km-oom-killed"     # never cluster-wide
    assert b["roleRef"]["name"] == "kubemedic-executor-actions" and b["subjects"][0]["name"] == "kubemedic-executor"
    assert agent_read_binding("km-oom-killed")["subjects"][0]["name"] == "kubemedic-agent"   # two separate identities
