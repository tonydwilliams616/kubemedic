"""The diagnosis contract: the one definition of what a valid agent answer looks like, shared by the agent, the lab and the evals.

    {
      "root_cause": "one or two sentences",
      "evidence": ["facts quoted from the evidence bundle", ...],
      "proposed_action": {"action": "rollback", "target": "Deployment/web", "reason": "..."},
      "confidence": "low" | "medium" | "high"
    }
"""
from __future__ import annotations

# The agent's whole action vocabulary. "escalate" means "hand to a human, change nothing".
# The Phase 3 executor will accept these and nothing else.
ACTIONS = ["rollout_restart", "rollback", "scale", "escalate"]
CONFIDENCE = {"low", "medium", "high"}


def validate(diag: object) -> list[str]:
    """Return every way the diagnosis breaks the contract; an empty list means valid."""
    if not isinstance(diag, dict):
        return ["diagnosis is not a JSON object"]
    problems = []
    if not isinstance(diag.get("root_cause"), str) or not diag["root_cause"].strip():
        problems.append("root_cause missing or empty")
    if not isinstance(diag.get("evidence"), list) or not all(isinstance(e, str) for e in diag["evidence"]):
        problems.append("evidence must be a list of strings")
    act = diag.get("proposed_action")
    if not isinstance(act, dict):
        problems.append("proposed_action missing")
    else:
        if act.get("action") not in ACTIONS:
            problems.append(f"action {act.get('action')!r} not in {ACTIONS}")
        if act.get("action") != "escalate" and not isinstance(act.get("target"), str):
            problems.append("target is required for any action other than escalate")
    if diag.get("confidence") not in CONFIDENCE:
        problems.append(f"confidence must be one of {sorted(CONFIDENCE)}")
    return problems


def validate_citations(diag: dict, allowed_ids: list[str]) -> list[str]:
    """When runbooks were supplied, the answer must cite at least one of them and nothing else."""
    cites = diag.get("citations")
    if not isinstance(cites, list) or not cites or not all(isinstance(c, str) for c in cites):
        return ["citations must be a non-empty list of runbook ids"]
    invented = [c for c in cites if c not in allowed_ids]
    return [f"citations not among the supplied runbooks: {invented}"] if invented else []
