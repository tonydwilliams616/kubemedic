"""Unit tests for the fault checker, using hand-built kubectl-shaped JSON."""
from lab.check import evaluate


def pod(name, waiting=None, terminated=None, last_terminated=None, conditions=()):
    state, last = {}, {}
    if waiting:
        state["waiting"] = {"reason": waiting}
    if terminated:
        state["terminated"] = {"reason": terminated}
    if last_terminated:
        last["terminated"] = {"reason": last_terminated}
    return {
        "metadata": {"name": name},
        "status": {
            "containerStatuses": [{"name": "c", "state": state, "lastState": last}],
            "conditions": [dict(c) for c in conditions],
        },
    }


def event(pod_name, reason, message=""):
    return {"involvedObject": {"kind": "Pod", "name": pod_name}, "reason": reason, "message": message}


def test_no_pods_is_not_ok():
    assert not evaluate([], [], {"waiting_reason": ["ImagePullBackOff"]}).ok


def test_waiting_reason_matches_any_listed():
    r = evaluate([pod("web-1", waiting="ErrImagePull")], [], {"waiting_reason": ["ErrImagePull", "ImagePullBackOff"]})
    assert r.ok and r.pod == "web-1"


def test_healthy_old_pod_does_not_hide_broken_new_pod():
    pods = [pod("web-old"), pod("web-new", waiting="ImagePullBackOff")]
    assert evaluate(pods, [], {"waiting_reason": ["ImagePullBackOff"]}).pod == "web-new"


def test_oom_seen_in_last_state_while_waiting_in_backoff():
    p = pod("worker-1", waiting="CrashLoopBackOff", last_terminated="OOMKilled")
    assert evaluate([p], [], {"terminated_reason": ["OOMKilled"]}).ok


def test_condition_reason_must_match_when_given():
    p = pod("r-1", conditions=[{"type": "PodScheduled", "status": "False", "reason": "SomethingElse"}])
    obs = {"pod_condition": {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}}
    assert not evaluate([p], [], obs).ok


def test_event_must_belong_to_the_same_pod():
    obs = {
        "pod_condition": {"type": "Ready", "status": "False"},
        "event": {"reason": "Unhealthy", "message_contains": "Readiness probe failed"},
    }
    not_ready = pod("fe-new", conditions=[{"type": "Ready", "status": "False"}])
    ready = pod("fe-old", conditions=[{"type": "Ready", "status": "True"}])
    wrong_pod_event = [event("fe-old", "Unhealthy", "Readiness probe failed: 404")]
    assert not evaluate([not_ready, ready], wrong_pod_event, obs).ok

    right_pod_event = [event("fe-new", "Unhealthy", "Readiness probe failed: HTTP probe failed with statuscode: 404")]
    assert evaluate([not_ready, ready], right_pod_event, obs).ok


def test_failure_detail_explains_closest_pod():
    r = evaluate([pod("a", waiting="ContainerCreating")], [], {"waiting_reason": ["CrashLoopBackOff"]})
    assert not r.ok and "ContainerCreating" in r.detail
