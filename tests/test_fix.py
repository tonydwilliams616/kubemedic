"""Checks the only path to a change: a proposal, a dry run, a typed human approval and a fresh re-check, with every step in an intact audit log."""
import copy
import tempfile
from pathlib import Path

import pytest

from kubemedic.audit import AuditLog, verify
from kubemedic.diagnose import Result
from kubemedic.executor import Approval, Executor, Refused, plan, rollout_done
from kubemedic.fix import run_fix
from test_agent import kubectl, rs
from test_tools import NS


def diagnosed(action="rollback", target="Deployment/worker", valid=True):
    proposal = {"action": action, "reason": "the current revision broke it"}
    if target:
        proposal["target"] = target
    diag = {"root_cause": "CACHE_MB=200 exceeds the 64Mi limit", "evidence": ["OOMKilled"],
            "proposed_action": proposal, "confidence": "high"}
    return lambda ns: Result(model_id="fake", diagnosis=diag if valid else None, valid=valid, prompt_version="v3",
                             problems=[] if valid else ["reply was not valid JSON"])


HEALTHY = {"replicas": 1, "updatedReplicas": 1, "availableReplicas": 1, "readyReplicas": 1, "observedGeneration": 3}


def setup(revisions=(1, 2), heals=True):
    """A fake cluster whose Deployment becomes healthy (or not) once a real change is applied,
    and a fake clock, so verification never really waits."""
    k = kubectl(revisions)
    dep = copy.deepcopy(k.objects[("deployment", "worker")])      # this test's own copy
    k.objects[("deployment", "worker")] = dep
    real_run = k.run

    def run(*args):
        if args[:1] == ("rollout",) and "--dry-run=server" not in args and heals:
            dep["metadata"]["generation"], dep["status"] = 3, dict(HEALTHY)
        return real_run(*args)
    k.run = run
    now = [0.0]
    ex = Executor(kubectl=k, verify_timeout_s=30, poll_s=3, clock=lambda: now[0],
                  sleep=lambda s: now.__setitem__(0, now[0] + s))
    return k, ex, AuditLog(Path(tempfile.mkdtemp()) / "audit.jsonl")


def real_changes(k):
    """kubectl commands that were NOT dry runs."""
    return [c for c in k.calls if c[0] == "run" and c[1] == "rollout" and "--dry-run=server" not in c]


def events(audit):
    return [r["event"] for r in audit.records()]


def test_typing_the_name_approves_and_the_change_is_applied_and_audited():
    k, ex, audit = setup()
    code = run_fix(NS, diagnosed(), ex, audit, ask=lambda q: "worker", approver="anthony", out=lambda s: None)
    assert code == 0
    assert real_changes(k) == [("run", "rollout", "undo", "deployment/worker", "-n", NS)]
    assert events(audit) == ["proposed", "dry_run", "approved", "executed", "verified"]
    executed = audit.records()[-2]
    assert executed["approver"] == "anthony" and executed["as_user"].endswith(":kubemedic-executor")
    assert verify(audit.path) == []


@pytest.mark.parametrize("typed", ["y", "yes", "", "Worker", "worker2"])
def test_anything_but_the_exact_name_rejects_and_changes_nothing(typed):
    k, ex, audit = setup()
    assert run_fix(NS, diagnosed(), ex, audit, ask=lambda q: typed, out=lambda s: None) == 1
    assert real_changes(k) == [] and events(audit)[-1] == "rejected"


def test_no_one_at_the_terminal_means_no_approval():
    def eof(q):
        raise EOFError
    k, ex, audit = setup()
    assert run_fix(NS, diagnosed(), ex, audit, ask=eof, out=lambda s: None) == 1 and real_changes(k) == []


def test_escalate_changes_nothing_and_never_asks():
    k, ex, audit = setup()
    asked = []
    run_fix(NS, diagnosed("escalate", target=None), ex, audit, ask=asked.append, out=lambda s: None)
    assert asked == [] and real_changes(k) == [] and events(audit) == ["proposed", "escalated"]


def test_a_refused_plan_never_reaches_the_human():
    k, ex, audit = setup()
    asked = []
    assert run_fix(NS, diagnosed("scale"), ex, audit, ask=asked.append, out=lambda s: None) == 1
    assert asked == [] and events(audit) == ["proposed", "refused_at_plan"]


def test_a_failed_precondition_is_refused_before_approval():
    k, ex, audit = setup(revisions=(2,))       # nothing to roll back to
    asked = []
    assert run_fix(NS, diagnosed(), ex, audit, ask=asked.append, out=lambda s: None) == 1
    assert asked == [] and events(audit) == ["proposed", "dry_run"] and not audit.records()[-1]["ok"]


def test_if_the_cluster_changes_while_the_human_reads_the_approval_is_void():
    k, ex, audit = setup()

    def someone_deploys_while_we_read(question):
        # A new release lands between the dry run and the approval.
        k.objects[("replicasets",)]["items"].append(rs(3))
        k.objects[("deployment", "worker")]["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "3"
        return "worker"

    assert run_fix(NS, diagnosed(), ex, audit, ask=someone_deploys_while_we_read, out=lambda s: None) == 1
    assert real_changes(k) == [] and events(audit) == ["proposed", "dry_run", "approved", "refused_at_apply"]
    assert "changed after approval" in audit.records()[-1]["problems"][0]


def test_no_valid_diagnosis_means_nothing_happens():
    k, ex, audit = setup()
    assert run_fix(NS, diagnosed(valid=False), ex, audit, ask=lambda q: "worker", out=lambda s: None) == 1
    assert events(audit) == ["proposed"] and k.calls == []


def test_apply_needs_an_approval_object():
    k, ex, audit = setup()
    with pytest.raises(Refused, match="needs a human Approval"):
        ex.apply(plan({"action": "rollback", "target": "Deployment/worker"}, NS), "yes please", audit)
    assert real_changes(k) == []


def test_an_approval_for_a_different_state_is_refused():
    k, ex, audit = setup()
    p = plan({"action": "rollback", "target": "Deployment/worker"}, NS)
    out = ex.apply(p, Approval(approver="anthony", fingerprint="approved-something-else"), audit)
    assert not out.ok and real_changes(k) == [] and events(audit) == ["refused_at_apply"]



# ---------- verification: "accepted" is not "fixed" ----------

def test_a_change_that_does_not_heal_is_reported_and_not_retried():
    k, ex, audit = setup(heals=False)
    said = []
    assert run_fix(NS, diagnosed(), ex, audit, ask=lambda q: "worker", out=said.append) == 1
    assert events(audit)[-2:] == ["executed", "verification_failed"]
    assert audit.records()[-1]["waited_s"] == 30
    assert len(real_changes(k)) == 1                     # exactly one change: no automatic second attempt
    assert any("NOT FIXED" in s and "needs a human" in s for s in said)


def test_verification_waits_for_the_rollout_to_finish():
    k, ex, audit = setup(heals=False)
    dep = k.objects[("deployment", "worker")]
    polls = []

    def sleep(s):                                           # the rollout completes on the third poll
        polls.append(s)
        if len(polls) == 2:
            dep["metadata"]["generation"], dep["status"] = 3, dict(HEALTHY)
    ex.sleep = sleep
    v = ex.verify(plan({"action": "rollback", "target": "Deployment/worker"}, NS), audit)
    # healthy from the third look, then it must STAY healthy for 3 looks in a row
    assert v.healthy and len(polls) == 4 and events(audit) == ["verified"] and "stable for 3 checks" in v.detail


def test_healthy_for_a_moment_is_not_verified():
    k, ex, audit = setup(heals=False)
    dep = k.objects[("deployment", "worker")]
    looks, tick = [], ex.sleep

    def flapping(s):                    # like a pod with no readiness probe: available, then it crashes again
        tick(s)                         # keep the fake clock moving so the timeout is reached
        looks.append(s)
        dep["metadata"]["generation"] = 3
        dep["status"] = dict(HEALTHY) if len(looks) % 2 else {**HEALTHY, "availableReplicas": 0}
    ex.sleep = flapping
    v = ex.verify(plan({"action": "rollback", "target": "Deployment/worker"}, NS), audit)
    assert not v.healthy and events(audit) == ["verification_failed"]


def dep_with(generation, **status):
    return {"metadata": {"generation": generation}, "spec": {"replicas": 2}, "status": status}


def test_rollout_done_matches_what_kubectl_rollout_status_checks():
    assert rollout_done(dep_with(3, observedGeneration=3, replicas=2, updatedReplicas=2, availableReplicas=2))[0]
    # the controller hasn't seen the new spec yet
    assert not rollout_done(dep_with(4, observedGeneration=3, replicas=2, updatedReplicas=2, availableReplicas=2))[0]
    # old pods still running alongside the new ones
    assert not rollout_done(dep_with(3, observedGeneration=3, replicas=3, updatedReplicas=2, availableReplicas=2))[0]
    # new pods not available yet (e.g. crash-looping)
    assert not rollout_done(dep_with(3, observedGeneration=3, replicas=2, updatedReplicas=2, availableReplicas=1))[0]
    assert rollout_done(None) == (False, {"error": "Deployment not found"})
