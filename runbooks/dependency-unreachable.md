---
id: dependency-unreachable
title: App fails because a service it depends on is unreachable
signals: ["connection refused", "no such host", "i/o timeout", "context deadline exceeded", "ECONNREFUSED", "no endpoints available", "503"]
mitigations: [rollback, rollout_restart, escalate]
related: [crashloopbackoff, readiness-probe-failing]
---
## Symptoms
- App logs show `connection refused`, `no such host`, timeouts or 503s when calling another service or database.
- The app may crash-loop, fail its readiness probe, or stay up but return errors.
- Often several different apps that share the dependency fail at once.

## Likely causes
1. **The dependency is down or scaled to zero**: its Service has no ready endpoints.
2. **DNS or Service name wrong**, often after a rename in a release (`no such host`).
3. **Network policy** newly blocking the traffic.
4. **Credentials** for the dependency rejected (look for 401/403 rather than connection errors).

## How to confirm
- Identify the host and port in the error message and check whether that Service has ready endpoints.
- Check whether the failing app's current revision changed the address it calls.
- If the dependency was scaled down deliberately (maintenance), that's a human decision, not a fault for the agent to undo.

## Mitigation
- **escalate** in most cases: the cause is outside the failing workload, and scaling up someone else's dependency could undo deliberate maintenance.
- **rollout_restart** of the failing app only after the dependency has recovered, if the app does not reconnect on its own.
- Rolling back the failing app only helps if its current revision changed the address it calls.

## Permanent fix (needs a human)
- Restore the dependency, fix the Service name or network policy, and make the app retry with backoff instead of exiting.
