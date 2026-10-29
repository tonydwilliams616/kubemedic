---
id: crashloopbackoff
title: Container keeps crashing and restarting (CrashLoopBackOff)
signals: [CrashLoopBackOff, "Back-off restarting failed container", "exit code 1", "Error", restartCount]
mitigations: [rollback, rollout_restart, escalate]
related: [oomkilled, liveness-probe-restarts, dependency-unreachable, createcontainerconfigerror]
---
## Symptoms
- The container starts, exits, and is restarted with growing delays (10s, 20s, 40s ... up to 5 minutes).
- `restartCount` keeps rising; the pod flips between `Running`, `Error` and `CrashLoopBackOff`.
- Without a readiness probe, a crashing new pod can briefly look Ready, so a rollout may complete and remove the old pods: a **full outage**.

## Likely causes
1. **The application fails at startup**: missing or wrong config file, bad environment variable, wrong command or arguments. Usually exit code 1 with a clear message in the logs.
2. **Killed for memory**: last state `OOMKilled`, exit code 137. See `oomkilled`.
3. **Killed by a failing liveness probe**: events say `Liveness probe failed`. See `liveness-probe-restarts`.
4. **A dependency is down** (database, another service) and the app exits instead of retrying. See `dependency-unreachable`.

## How to confirm
- Read the **previous** container's logs (`kubectl logs --previous`); the last lines before exit usually name the problem.
- Check `lastState.terminated`: reason and exit code (1 = application error, 137 = killed with SIGKILL, often memory, 143 = SIGTERM).
- Compare what the current revision changed (env, args, mounts) with the error in the logs. A path in the logs that does not match the keys of a mounted ConfigMap is a common cause.

## Mitigation
- **rollback** when the crashes started with the current revision and an earlier revision exists.
- **rollout_restart** only for a transient cause that has since cleared (for example a dependency that has recovered). If the cause is in the pod spec, a restart repeats the crash.
- **escalate** when the cause is outside the Deployment (a dependency, a missing Secret) or there is no earlier revision.

## Permanent fix (needs a human)
- Fix the configuration or code in the release; add a readiness probe so a crashing release cannot replace healthy pods.
