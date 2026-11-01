"""Turns a proposed action into exactly one allowlisted change, run as the executor's own narrow identity, and only with a human approval bound to the cluster state they saw.

Separation of duties:
- The diagnosing agent (read-only identity) PROPOSES. RBAC stops it changing anything.
- The executor (ServiceAccount kubemedic-executor, deploy/executor/rbac.yaml) can ACT, but only
  rollout_restart and rollback of a Deployment, in namespaces the lab has bound its role to.

What the executor never takes from the model: a command, a kubectl flag or a patch. The proposal
supplies an action name from a fixed menu and a Deployment name that must be a valid Kubernetes
name; the executor builds the one command for that action itself.

Preconditions are re-checked against the LIVE cluster at execution time, never taken from the
diagnosis: time passes between a proposal and its approval, and the cluster can change in
between (a "time of check to time of use" gap).

Not supported, on purpose:
- scale: the diagnosis contract carries no replica count, and guessing one could cause an outage.
- anything else: not on the allowlist, so refused.

Two ways in, and only two:
- dry_run(plan): the API server validates the change without applying it (--dry-run=server).
- apply(plan, approval, audit): the real change. It needs an Approval whose fingerprint matches the
  cluster state RIGHT NOW (if anything changed since the human approved, it refuses), and it writes
  the audit record itself, so a change can't happen without being logged.

Usage: python -m kubemedic.executor --namespace km-bad-image-tag --action rollback --target Deployment/web
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field

from . import guardrails
from .evidence import AgentKubectl, summarise_deployment
from .tools import NAME

EXECUTOR_USER = "system:serviceaccount:kubemedic-system:kubemedic-executor"
SUPPORTED = ("rollout_restart", "rollback")


class Refused(Exception):
    """The executor will not do this; the message says why."""


class ExecutorKubectl(AgentKubectl):
    """kubectl as the executor's ServiceAccount: only deploy/executor/rbac.yaml applies."""
    user = EXECUTOR_USER


@dataclass
class Plan:
    action: str                    # rollout_restart | rollback | escalate
    namespace: str
    deployment: str | None         # None for escalate
    command: list[str] = field(default_factory=list)   # kubectl arguments, built here; empty for escalate
    reason: str = ""               # the proposal's reason, shown to whoever approves it


@dataclass
class Outcome:
    plan: Plan
    ok: bool
    changed: bool                  # did anything in the cluster change? Always False for a dry run
    summary: str                   # what would change (or did), in words
    refused: list[str] = field(default_factory=list)
    output: str = ""               # what kubectl said
    fingerprint: str = ""          # the cluster state this outcome was based on (see fingerprint())


@dataclass(frozen=True)
class Approval:
    """A human's yes to ONE plan in ONE cluster state. It can't be reused for anything else."""
    approver: str
    fingerprint: str               # must equal fingerprint(plan, live state) at the moment of applying


def plan(proposal: dict, namespace: str) -> Plan:
    """Check a proposed action against the allowlist and build its one command. Raises Refused."""
    action, reason = proposal.get("action"), proposal.get("reason", "")
    if not re.fullmatch(NAME["pattern"], namespace or ""):
        raise Refused(f"namespace {namespace!r} is not a valid Kubernetes name")
    if action == "escalate":
        return Plan("escalate", namespace, None, [], reason)
    if action == "scale":
        raise Refused("scale is not supported: the proposal carries no replica count, and guessing one could cause an outage")
    if action not in SUPPORTED:
        raise Refused(f"action {action!r} is not on the executor's allowlist {list(SUPPORTED)}")
    kind, _, name = (proposal.get("target") or "").partition("/")
    if kind != "Deployment" or not re.fullmatch(NAME["pattern"], name):
        raise Refused(f"target {proposal.get('target')!r} must be Deployment/<valid name>")
    verb = {"rollout_restart": "restart", "rollback": "undo"}[action]
    return Plan(action, namespace, name, ["rollout", verb, f"deployment/{name}", "-n", namespace], reason)


class Executor:
    def __init__(self, context: str = "kind-kubemedic", kubectl: AgentKubectl | None = None):
        self.k = kubectl or ExecutorKubectl(context)   # tests pass a fake, so no cluster is needed

    def live_deployment(self, p: Plan) -> dict | None:
        """The target as it is NOW, read with the executor's own identity."""
        dep = self.k.get(p.namespace, "deployment", p.deployment)
        if dep is None:
            return None
        rs = self.k.get(p.namespace, "replicasets") or {"items": []}
        return summarise_deployment(dep, rs["items"])

    def check(self, p: Plan) -> tuple[list[str], str, str]:
        """Re-check preconditions on the live cluster. Returns (problems, what would change, state fingerprint)."""
        dep = self.live_deployment(p)
        if dep is None:
            return [f"Deployment {p.deployment!r} not found in {p.namespace} (or not permitted)"], "", ""
        # The same rules the diagnoser's guardrails apply, now against fresh facts.
        proposal = {"proposed_action": {"action": p.action, "target": f"Deployment/{p.deployment}"}}
        problems = guardrails.check(proposal, {"deployments": [dep]})
        return problems, describe(p, dep), fingerprint(p, dep)

    def dry_run(self, p: Plan) -> Outcome:
        """Ask the API server to validate the change without applying it (kubectl --dry-run=server)."""
        if p.action == "escalate":
            return Outcome(p, ok=True, changed=False, summary="escalate: hand over to a human; nothing changes")
        problems, summary, state = self.check(p)
        if problems:
            return Outcome(p, ok=False, changed=False, summary=summary, refused=problems, fingerprint=state)
        ok, out = self.k.run(*p.command, "--dry-run=server")
        return Outcome(p, ok=ok, changed=False, summary=summary, output=out.strip(), fingerprint=state,
                       refused=[] if ok else [f"the API server rejected the dry run: {out.strip()}"])

    def apply(self, p: Plan, approval: Approval, audit) -> Outcome:
        """Make the real change, only if the approval matches the cluster as it is now. Always audited."""
        if not isinstance(approval, Approval):
            raise Refused("a real change needs a human Approval")
        if p.action == "escalate":
            raise Refused("escalate changes nothing; there is nothing to apply")
        problems, summary, state = self.check(p)       # check AGAIN: time passed while the human read it
        if not problems and state != approval.fingerprint:
            problems = ["the cluster changed after approval (the approved state no longer matches); "
                        "nothing was changed: review and approve again"]
        if problems:
            audit.append("refused_at_apply", namespace=p.namespace, action=p.action, deployment=p.deployment,
                         approver=approval.approver, problems=problems)
            return Outcome(p, ok=False, changed=False, summary=summary, refused=problems, fingerprint=state)
        ok, out = self.k.run(*p.command)
        audit.append("executed", namespace=p.namespace, action=p.action, deployment=p.deployment,
                     command=["kubectl", *p.command], as_user=EXECUTOR_USER, approver=approval.approver,
                     ok=ok, output=out.strip()[:500])
        return Outcome(p, ok=ok, changed=ok, summary=summary, output=out.strip(), fingerprint=state,
                       refused=[] if ok else [f"kubectl failed: {out.strip()}"])


def fingerprint(p: Plan, dep: dict) -> str:
    """A short hash of the plan plus the parts of the live Deployment it depends on.

    The approval is bound to this: if the current revision, its images or the desired replica
    count change between approval and apply, the fingerprint changes and the apply is refused.
    """
    history = dep.get("rollout_history", [])
    state = {"plan": [p.action, p.namespace, p.deployment, p.command],
             "revisions": [[r.get("revision"), r.get("current"), r.get("images")] for r in history],
             "desired": dep.get("replicas", {}).get("desired")}
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]


def describe(p: Plan, dep: dict) -> str:
    """What the change means, in words an approver can check."""
    history = dep.get("rollout_history", [])
    current = next((r for r in history if r.get("current")), None)
    if p.action == "rollout_restart":
        return (f"restart every pod of Deployment {p.deployment} (same spec, new pods), "
                f"currently {dep['replicas'].get('readyReplicas', 0)}/{dep['replicas'].get('desired')} ready")
    earlier = [r for r in history if not r.get("current")]
    target = earlier[-1] if earlier else None
    if not (current and target):
        return f"roll back Deployment {p.deployment}: no earlier revision found"
    undo = "; ".join(current.get("changes_from_previous") or []) or "no recorded changes"
    return (f"roll back Deployment {p.deployment} from revision {current['revision']} "
            f"({', '.join(current.get('images') or [])}) to revision {target['revision']} "
            f"({', '.join(target.get('images') or [])}), undoing: {undo}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Dry-run one allowlisted fix as the executor's identity")
    p.add_argument("--namespace", "-n", required=True)
    p.add_argument("--action", required=True)
    p.add_argument("--target", default=None, help="Deployment/<name> (not needed for escalate)")
    p.add_argument("--context", default="kind-kubemedic")
    args = p.parse_args(argv)
    try:
        the_plan = plan({"action": args.action, "target": args.target, "reason": "requested from the CLI"},
                        args.namespace)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    out = Executor(args.context).dry_run(the_plan)
    print(f"plan:    {' '.join(['kubectl', *the_plan.command]) if the_plan.command else '(no command)'}")
    print(f"meaning: {out.summary}")
    if out.refused:
        print("REFUSED: " + "; ".join(out.refused))
        return 1
    print(f"dry run: {out.output or 'ok'} (nothing was changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
