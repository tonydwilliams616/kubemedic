---
id: readiness-probe-failing
title: Pods run but never become Ready (readiness probe failing)
signals: ["Readiness probe failed", Unhealthy, "statuscode: 404", "statuscode: 503", "connection refused", "Ready=False", ProgressDeadlineExceeded]
mitigations: [rollback, escalate]
related: [rollout-stuck, liveness-probe-restarts, dependency-unreachable]
---
## Symptoms
- Containers are `Running` but the pod's `Ready` condition is `False`; restarts stay at 0.
- Events: `Unhealthy ... Readiness probe failed: HTTP probe failed with statuscode: 404` (or 503, timeouts, connection refused).
- The pod receives no Service traffic. In a rollout, old pods keep serving and the rollout stalls: usually a **partial** problem, not an outage.

## Likely causes
1. **The probe points at the wrong place**: wrong path (404) or port (connection refused), often changed in a release.
2. **The app isn't ready yet**: slow start with too short `initialDelaySeconds`, or a too-tight timeout.
3. **The app reports itself unhealthy (503)** because a dependency is down. See `dependency-unreachable`.

## How to confirm
- Read the probe failure message: the status code or error says which cause applies.
- Compare the probe in the current revision with the previous revision.
- Check the app's own logs for requests to the probe path.

## Mitigation
- **rollback** when the current revision changed the probe or the app and an earlier revision exists. Old pods are still serving, so this is lower urgency than an outage, but it unblocks the release.
- **escalate** when a dependency is the cause, or when no revision change explains it.
- `rollout_restart` does not help: new pods are probed the same way.

## Permanent fix (needs a human)
- Point the probe at an endpoint the app really serves, or tune its timings.
