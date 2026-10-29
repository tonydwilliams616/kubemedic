---
id: rollout-stuck
title: Deployment rollout is stuck (ProgressDeadlineExceeded)
signals: [ProgressDeadlineExceeded, "has timed out progressing", "Progressing=False", updatedReplicas, unavailableReplicas]
mitigations: [rollback, escalate]
related: [image-pull-errors, readiness-probe-failing, crashloopbackoff, pending-unschedulable]
---
## Symptoms
- The Deployment's `Progressing` condition becomes `False` with reason `ProgressDeadlineExceeded` (default after 600 seconds).
- New ReplicaSet pods are not becoming available; old ReplicaSet pods are usually still serving.

## Likely causes
A stuck rollout is a **symptom, not a cause**. The new pods are failing for some other reason; find which:
- Image cannot be pulled: see `image-pull-errors`.
- Pods run but are not Ready: see `readiness-probe-failing`.
- Pods crash: see `crashloopbackoff` or `oomkilled`.
- Pods are Pending: see `pending-unschedulable`.

## How to confirm
- Look at the pods owned by the **newest** ReplicaSet (the current revision) and their waiting reason or events.
- Use the rollout history to see what the current revision changed.

## Mitigation
- **rollback** when the newest revision is the problem and an earlier revision exists; this is the standard response to a bad release.
- **escalate** when the cause is outside the Deployment.

## Permanent fix (needs a human)
- Fix whatever the underlying page identifies, then release again.
