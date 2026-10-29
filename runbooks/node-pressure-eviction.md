---
id: node-pressure-eviction
title: Pods evicted because a node ran low on memory or disk
signals: [Evicted, "The node was low on resource", MemoryPressure, DiskPressure, PIDPressure, "ephemeral-storage"]
mitigations: [escalate]
related: [oomkilled, pending-unschedulable]
---
## Symptoms
- Pods end with status `Evicted` and a message such as `The node was low on resource: memory`.
- The node shows a `MemoryPressure`, `DiskPressure` or `PIDPressure` condition.
- Unlike `OOMKilled`, the container did not exceed its own limit; the **node** as a whole ran short.

## Likely causes
1. Pods without memory requests or limits using more than the node can supply.
2. Logs or temporary files filling the node's disk (`ephemeral-storage`).
3. Too many pods packed onto too few nodes.

## How to confirm
- The pod's status reason is `Evicted` (not `OOMKilled`), and the message names the resource.
- Several unrelated pods on the same node are affected at about the same time.

## Mitigation
- **escalate**: this is a node or capacity problem. Rolling back or restarting one Deployment does not relieve the node and can move the pressure elsewhere.

## Permanent fix (needs a human)
- Set realistic requests and limits, clean up disk usage, or add node capacity.
