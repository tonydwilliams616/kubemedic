---
id: oomkilled
title: Container killed for exceeding its memory limit (OOMKilled)
signals: [OOMKilled, "exit code 137", "Exit Code: 137", "memory limit", "Out of memory"]
mitigations: [rollback, escalate]
related: [crashloopbackoff, node-pressure-eviction]
---
## Symptoms
- `lastState.terminated.reason: OOMKilled`, exit code 137, often followed by `CrashLoopBackOff`.
- The process is killed by the kernel the moment its memory use crosses the container's `resources.limits.memory`.

## Likely causes
1. **A change made the app use more memory**: a bigger cache, batch or buffer size, more threads, a larger dataset. The limit did not change; the usage did.
2. **The limit was lowered** in the new revision.
3. **A memory leak**: usage grows over hours, then the container is killed; restarts buy time but it returns.
4. **Not this page**: if the pod was `Evicted` rather than OOMKilled, the *node* ran out of memory. See `node-pressure-eviction`.

## How to confirm
- Compare the container's memory limit with what the app is configured to use. Units matter: `Mi` = mebibytes, `Gi` = gibibytes.
- Look at what the current revision changed: a size-related setting (cache, heap, batch, buffer) or the limit itself.
- If nothing changed and restarts are hours apart, suspect a leak.

## Mitigation
- **rollback** when the current revision raised memory use or lowered the limit, and an earlier revision exists.
- **escalate** for leaks or when no revision change explains it: the real fix is a limit or code change.
- Never `rollout_restart` or `scale` for a configuration-driven OOM: every new pod has the same config and is killed the same way.

## Permanent fix (needs a human)
- Either raise `resources.limits.memory` to fit the intended workload, or reduce the app's memory use (for example a smaller cache size).
