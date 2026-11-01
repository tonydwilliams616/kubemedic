"""Diagnoses a namespace, shows the proposed fix to a human, and applies it only if they approve it, recording every step in the audit log.

The only path to a change in the cluster:
  1. diagnose      the read-only agent (or one-shot diagnoser) proposes an action   -> "proposed"
  2. plan          the executor checks it against its allowlist and builds the command
                   (refused -> "refused_at_plan"; escalate -> "escalated", nothing changes)
  3. dry run       the API server validates the change as the executor's identity  -> "dry_run"
  4. approval      a human reads the proposal and TYPES the Deployment's name        -> "approved" / "rejected"
                   (typing the name, not pressing y, stops reflexive approvals)
  5. apply         the executor re-checks the live cluster; if it changed since the
                   human approved, it refuses; otherwise it acts                    -> "executed" / "refused_at_apply"

There is no flag to skip step 4, and approval needs a person at a terminal: a script, CI job or
pipe can't approve anything.

Usage: python -m kubemedic.fix --namespace km-bad-image-tag [--agent] [--rag] [--model MODEL_ID]
"""
from __future__ import annotations

import argparse
import getpass
import sys
import textwrap
from typing import Callable

from .audit import AuditLog
from .executor import Approval, Executor, Refused, plan


def run_fix(namespace: str, diagnose_fn: Callable[[str], object], executor: Executor, audit: AuditLog,
            ask: Callable[[str], str] = input, approver: str | None = None, out: Callable[[str], None] = print) -> int:
    """The whole proposal -> approval -> change workflow. Returns 0 if it ended as intended."""
    result = diagnose_fn(namespace)
    diag = result.diagnosis or {}
    proposal = diag.get("proposed_action") or {}
    audit.append("proposed", namespace=namespace, model=result.model_id, prompt_version=result.prompt_version,
                 valid=result.valid, root_cause=diag.get("root_cause"), confidence=diag.get("confidence"),
                 proposed_action=proposal, guardrail_override=result.overridden)
    if not result.valid:
        out(f"No valid diagnosis ({'; '.join(result.problems)}). Nothing will change.")
        return 1

    out("\nDIAGNOSIS")
    out(textwrap.fill(diag["root_cause"], 96, initial_indent="  ", subsequent_indent="  "))
    out(f"  confidence: {diag.get('confidence')}   proposed: {proposal.get('action')} {proposal.get('target') or ''}")
    out(textwrap.fill("reason: " + proposal.get("reason", ""), 96, initial_indent="  ", subsequent_indent="  "))

    try:
        p = plan(proposal, namespace)
    except Refused as e:
        audit.append("refused_at_plan", namespace=namespace, proposed_action=proposal, reason=str(e))
        out(f"\nREFUSED by the executor: {e}. Nothing will change.")
        return 1
    if p.action == "escalate":
        audit.append("escalated", namespace=namespace, reason=proposal.get("reason"))
        out("\nESCALATED: the agent recommends a human takes this on. Nothing will change.")
        return 0

    dry = executor.dry_run(p)
    audit.append("dry_run", namespace=namespace, action=p.action, deployment=p.deployment,
                 command=["kubectl", *p.command], ok=dry.ok, summary=dry.summary, refused=dry.refused,
                 fingerprint=dry.fingerprint, output=dry.output[:500])
    if not dry.ok:
        out(f"\nREFUSED before approval: {'; '.join(dry.refused)}. Nothing will change.")
        return 1

    out("\nPROPOSED CHANGE (validated by a server-side dry run, not yet applied)")
    out(f"  command:  kubectl {' '.join(p.command)}   (run as the executor's own identity)")
    out(textwrap.fill("effect:   " + dry.summary, 96, initial_indent="  ", subsequent_indent="            "))
    who = approver or getpass.getuser()
    try:
        typed = ask(f"\nTo approve, type the Deployment name ({p.deployment}); anything else rejects: ").strip()
    except EOFError:
        typed = ""
    if typed != p.deployment:
        audit.append("rejected", namespace=namespace, action=p.action, deployment=p.deployment, approver=who,
                     typed=typed[:100])
        out("Rejected. Nothing changed.")
        return 1
    approval = Approval(approver=who, fingerprint=dry.fingerprint)
    audit.append("approved", namespace=namespace, action=p.action, deployment=p.deployment, approver=who,
                 fingerprint=dry.fingerprint)

    done = executor.apply(p, approval, audit)      # re-checks the live cluster, then acts; audits itself
    if not done.ok:
        out(f"NOT APPLIED: {'; '.join(done.refused)}")
        return 1
    out(f"APPLIED: {done.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from botocore.exceptions import BotoCoreError, ClientError

    from .diagnose import DEFAULT_MODEL, BedrockModel, diagnose
    from .evidence import AgentKubectl, gather

    p = argparse.ArgumentParser(description="Diagnose, propose, and (only with your typed approval) fix a namespace")
    p.add_argument("--namespace", "-n", required=True)
    p.add_argument("--context", default="kind-kubemedic")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--agent", action="store_true", help="diagnose with the tool-using agent instead of one-shot")
    p.add_argument("--rag", action="store_true", help="use the runbooks")
    args = p.parse_args(argv)

    if not sys.stdin.isatty():
        print("Approval needs a person at a terminal: stdin is not interactive, so nothing will be changed.",
              file=sys.stderr)
        return 2

    model = BedrockModel(args.model)
    retriever = None
    if args.rag:
        from .retrieve import Retriever
        retriever = Retriever()

    def diagnose_fn(namespace: str):
        if args.agent:
            from .agent import investigate
            from .tools import Toolbox
            return investigate(Toolbox(namespace, AgentKubectl(args.context), retriever), model)
        bundle = gather(namespace, args.context)
        return diagnose(bundle, model, runbooks=retriever.top_runbooks(bundle) if retriever else None)

    audit = AuditLog()
    try:
        code = run_fix(args.namespace, diagnose_fn, Executor(args.context), audit)
    except BotoCoreError as e:
        print(f"AWS credentials problem: {e}\nFix: source ./aws-login.sh", file=sys.stderr)
        return 2
    except ClientError as e:
        print(f"Bedrock error ({args.model}): {e.response['Error']['Message']}", file=sys.stderr)
        return 2
    print(f"\naudit log: {audit.path} (check it with: make audit-verify)")
    return code


if __name__ == "__main__":
    sys.exit(main())
