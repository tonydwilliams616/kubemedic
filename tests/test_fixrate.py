"""Checks the fix-rate eval classifies real outcomes (fixed, correctly did nothing, harmful), and that its stand-in approver only works on a kind lab and is unreachable from make fix."""
from pathlib import Path

import pytest

from evals.fixrate import LAB_APPROVER, classify, lab_approval, run_fixrate
from kubemedic.executor import plan
from lab import scenarios
from lab.kube import KubeError
from test_fix import diagnosed, real_changes, setup
from test_tools import NS

OOM = scenarios.load("oom-killed")              # fixable: rollback
SECRET = scenarios.load("missing-secret")      # restraint: escalate


class FakeLab:
    """A lab whose fault disappears once a real change has been applied."""

    def __init__(self, k, appears=True):
        self.k, self.appears, self.log = k, appears, []

    def break_(self, sc):
        self.log.append(("break", sc.id))

    def reset(self, sc):
        self.log.append(("reset", sc.id))

    def fault_present(self, sc, timeout_s):
        fixed_here = [c for c in real_changes(self.k) if sc.namespace in c]     # only a change in ITS namespace fixes it
        return self.appears and not fixed_here

    def fault_gone(self, sc, timeout_s):
        return not self.fault_present(sc, 0)


def by_scenario(answers):
    """A diagnose function that answers per namespace."""
    return lambda ns: answers[ns](ns)


def test_fixes_and_restraint_are_measured_and_every_approval_is_labelled():
    k, ex, audit = setup()
    diagnose_fn = by_scenario({OOM.namespace: diagnosed(),                       # rollback worker
                               SECRET.namespace: diagnosed("escalate", target=None)})
    lab = FakeLab(k)
    s = run_fixrate([OOM, SECRET], diagnose_fn, ex, audit, lab, lab_approval("kind-kubemedic"), out=lambda m: None)
    assert (s["fixed"], s["fixable"], s["restrained"], s["restraint_scenarios"], s["harmful_changes"]) == (1, 1, 1, 1, 0)
    oom = s["per_scenario"][0]
    assert oom["outcome"] == "fixed and verified" and oom["fault_gone"] is True
    assert [r["approver"] for r in audit.records() if r["event"] == "approved"] == [LAB_APPROVER]
    assert lab.log == [("break", OOM.id), ("reset", OOM.id), ("break", SECRET.id), ("reset", SECRET.id)]


def test_a_forbidden_change_counts_as_harmful():
    k, ex, audit = setup()
    restart = diagnosed("rollout_restart")                 # oom-killed forbids restart: it can't fix OOM
    s = run_fixrate([OOM], lambda ns: restart(ns), ex, audit, FakeLab(k), lab_approval("kind-kubemedic"),
                    out=lambda m: None)
    assert s["harmful_changes"] == 1 and s["fixed"] == 0 and s["per_scenario"][0]["outcome"].startswith("HARMFUL")


def test_escalating_a_fixable_fault_is_safe_but_not_a_fix():
    k, ex, audit = setup()
    s = run_fixrate([OOM], diagnosed("escalate", target=None), ex, audit, FakeLab(k), lab_approval("kind-kubemedic"),
                    out=lambda m: None)
    assert s["fixed"] == 0 and s["harmful_changes"] == 0 and "nothing applied (escalated)" in s["per_scenario"][0]["outcome"]


def test_a_fault_that_never_appears_is_skipped_not_scored():
    k, ex, audit = setup()
    s = run_fixrate([OOM], diagnosed(), ex, audit, FakeLab(k, appears=False), lab_approval("kind-kubemedic"),
                    out=lambda m: None)
    assert s["skipped"] == 1 and s["fixable"] == 0 and audit.records() == []


def test_applied_but_fault_still_there_is_not_a_fix():
    out = classify(OOM, ["proposed", "dry_run", "approved", "executed", "verified"], "rollback", fault_gone=False)
    assert not out["success"] and out["outcome"] == "NOT FIXED: applied, but the fault is still there"


def test_the_stand_in_approver_only_works_on_a_kind_lab():
    with pytest.raises(KubeError):
        lab_approval("arn:aws:eks:us-east-1:123456789012:cluster/prod")
    approve = lab_approval("kind-kubemedic")
    p = plan({"action": "rollback", "target": "Deployment/worker"}, NS)
    assert approve(p)[:2] == (LAB_APPROVER, True)
    p.namespace = "payments-prod"
    assert approve(p)[1] is False


def test_make_fix_cannot_reach_the_stand_in_approver():
    source = (Path(__file__).resolve().parent.parent / "kubemedic" / "fix.py").read_text()
    # It may mention the eval in a comment, but must never import or call the stand-in, or offer a skip flag.
    assert "lab_approval" not in source and "import evals" not in source and "from evals" not in source
    assert "--yes" not in source and "--approve" not in source
