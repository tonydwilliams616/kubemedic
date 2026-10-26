"""Decide whether a scenario's fault is actually observable in the cluster.

Pure functions over `kubectl get -o json` output, so they are unit-testable
without a cluster. All pod-level assertions (and the event, if any) must hold
for the SAME pod; that stops a stale pod from an earlier revision producing a
false pass.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CheckResult:
    ok: bool
    pod: str | None
    detail: str


def _container_statuses(pod: dict) -> list[dict]:
    status = pod.get("status", {})
    return status.get("containerStatuses", []) + status.get("initContainerStatuses", [])


def _waiting_reasons(pod: dict) -> set[str]:
    return {cs.get("state", {}).get("waiting", {}).get("reason")
            for cs in _container_statuses(pod)} - {None}


def _terminated_reasons(pod: dict) -> set[str]:
    reasons = set()
    for cs in _container_statuses(pod):
        for key in ("state", "lastState"):
            reason = cs.get(key, {}).get("terminated", {}).get("reason")
            if reason:
                reasons.add(reason)
    return reasons


def _condition_matches(pod: dict, want: dict) -> bool:
    for c in pod.get("status", {}).get("conditions", []):
        if c.get("type") != want["type"] or c.get("status") != want["status"]:
            continue
        if "reason" in want and c.get("reason") != want["reason"]:
            continue
        return True
    return False


def _event_matches(pod_name: str, events: list[dict], want: dict) -> bool:
    for e in events:
        obj = e.get("involvedObject", {})
        if obj.get("kind") != "Pod" or obj.get("name") != pod_name:
            continue
        if e.get("reason") != want["reason"]:
            continue
        if want.get("message_contains", "") not in (e.get("message") or ""):
            continue
        return True
    return False


def _pod_failures(pod: dict, events: list[dict], observable: dict) -> list[str]:
    name = pod["metadata"]["name"]
    failures = []
    if "waiting_reason" in observable:
        got = _waiting_reasons(pod)
        if not got & set(observable["waiting_reason"]):
            failures.append(f"waiting reason {sorted(got) or 'none'} not in {observable['waiting_reason']}")
    if "terminated_reason" in observable:
        got = _terminated_reasons(pod)
        if not got & set(observable["terminated_reason"]):
            failures.append(f"terminated reason {sorted(got) or 'none'} not in {observable['terminated_reason']}")
    if "pod_condition" in observable and not _condition_matches(pod, observable["pod_condition"]):
        failures.append(f"no condition matching {observable['pod_condition']}")
    if "event" in observable and not _event_matches(name, events, observable["event"]):
        failures.append(f"no event matching {observable['event']}")
    return failures


def evaluate(pods: list[dict], events: list[dict], observable: dict) -> CheckResult:
    if not pods:
        return CheckResult(False, None, "no pods match the target selector yet")
    closest = None
    for pod in pods:
        failures = _pod_failures(pod, events, observable)
        name = pod["metadata"]["name"]
        if not failures:
            return CheckResult(True, name, "fault observable")
        if closest is None or len(failures) < len(closest[1]):
            closest = (name, failures)
    name, failures = closest
    return CheckResult(False, name, "; ".join(failures))
