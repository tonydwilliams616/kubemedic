"""Checks a proposed action against hard facts in the evidence before it is accepted: prompts ask, code enforces.

The model is told to verify things, but it doesn't always (prompt v3 proposed a rollback with no
earlier revision to roll back to). These rules don't rely on the model's judgement; they read the
evidence bundle directly. Each rule is a fact about Kubernetes, not about our test scenarios:

1. The target must be a Deployment that actually appears in the evidence.
2. A rollback needs an earlier revision in that Deployment's rollout history.

What guardrails can NOT catch: bad judgement within the rules (for example, scaling when scaling
won't help). That is what human approval in the executor phase is for.
"""
from __future__ import annotations


def _deployment(bundle: dict, target: str) -> dict | None:
    """Find "Deployment/<name>" in the evidence; None if it isn't there."""
    kind, _, name = target.partition("/")
    if kind != "Deployment":
        return None
    return next((d for d in bundle.get("deployments", []) if d.get("name") == name), None)


def check(diagnosis: dict, bundle: dict) -> list[str]:
    """Return every rule the proposed action breaks; an empty list means it may go ahead."""
    action = diagnosis["proposed_action"]["action"]
    if action == "escalate":
        return []                                  # changing nothing is always allowed

    target = diagnosis["proposed_action"].get("target") or ""
    dep = _deployment(bundle, target)
    if dep is None:
        return [f"target {target!r} is not a Deployment in the evidence"]

    violations = []
    if action == "rollback":
        earlier = [r for r in dep.get("rollout_history", []) if not r.get("current")]
        if not earlier:
            violations.append(f"rollback needs an earlier revision, but {target} has only its current revision "
                              "in rollout_history")
    return violations
