---
id: pending-unschedulable
title: Pod stuck Pending because the scheduler cannot place it (FailedScheduling)
signals: [Pending, FailedScheduling, Unschedulable, "Insufficient cpu", "Insufficient memory", "untolerated taint", "didn't match Pod's node affinity", "unbound immediate PersistentVolumeClaims"]
mitigations: [rollback, escalate]
related: [rollout-stuck]
---
## Symptoms
- Pod stays `Pending` with condition `PodScheduled=False, reason=Unschedulable`.
- Event `FailedScheduling`, for example: `0/3 nodes are available: 2 Insufficient cpu, 1 node(s) had untolerated taint ...`.

## Reading the FailedScheduling message
The message gives **one reason per node**, so several reasons usually appear together. Only one is normally the real problem:
- A `node-role.kubernetes.io/control-plane` taint is **expected**: workloads don't run on control-plane nodes by design. It only explains why that one node was skipped.
- Focus on the reason given for the **worker** nodes.

## Likely causes
1. **The request is bigger than any node can offer.** Check units carefully: CPU `1` = one whole core, `500m` = half a core, `64m` = 0.064 of a core, but `64` = sixty-four cores. A missing `m` is a classic mistake. Memory: `Mi`/`Gi`.
2. **The cluster is genuinely full**: the request is reasonable but other pods use the capacity.
3. **Placement rules exclude every node**: `nodeSelector`, node affinity, or taints without matching tolerations.
4. **Storage**: a PersistentVolumeClaim that cannot bind.

## How to confirm
- Compare the pod's `resources.requests` with a typical node's allocatable CPU and memory. A request larger than any single node can never be scheduled, no matter how empty the cluster is.
- If the request is reasonable, check how full the nodes are.

## Mitigation
- **rollback** when the current revision introduced the oversized request or the placement rule, and an earlier revision exists.
- **escalate** otherwise: fixing the request, adding nodes, or changing placement rules is a human decision.
- Never `scale` up: more replicas just add more Pending pods. Adding nodes does not help a request bigger than any node.

## Permanent fix (needs a human)
- Correct the request (often a missing `m`), resize the node pool, or fix the placement rules.
