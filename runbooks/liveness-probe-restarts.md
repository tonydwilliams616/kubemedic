---
id: liveness-probe-restarts
title: Containers restarted by a failing liveness probe
signals: ["Liveness probe failed", "failed liveness probe, will be restarted", Killing, "exit code 137", "exit code 143", restartCount]
mitigations: [rollback, rollout_restart, escalate]
related: [crashloopbackoff, readiness-probe-failing, oomkilled]
---
## Symptoms
- Restarts keep rising even though the app itself did not crash.
- Events: `Liveness probe failed: ...` then `Container <name> failed liveness probe, will be restarted`.
- The killed container shows exit code 143 (stopped with SIGTERM) or 137 (SIGKILL after the grace period). 137 looks like an OOM kill, but the reason is **not** `OOMKilled`.

## Likely causes
1. **The probe is wrong**: wrong path or port, or too aggressive (short timeout, low `failureThreshold`), often introduced by a release.
2. **The app hangs** (deadlock, exhausted thread pool) and genuinely stops answering.
3. **Slow startup** without a `startupProbe`: the app is killed before it finishes starting.

## How to confirm
- Exit code 137 or 143 plus `Liveness probe failed` events means the kubelet killed it, not the kernel's memory limit. (`OOMKilled` means memory; see `oomkilled`.)
- Compare the liveness probe in the current revision with the previous one.

## Mitigation
- **rollback** when the current revision changed the probe or introduced the hang.
- **rollout_restart** can buy time for an intermittent hang that a fresh pod clears, but treat it as temporary.
- **escalate** when neither applies.

## Permanent fix (needs a human)
- Fix the probe or its timings, add a `startupProbe` for slow starters, or fix the hang in the code.
