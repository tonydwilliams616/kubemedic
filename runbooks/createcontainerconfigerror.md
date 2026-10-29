---
id: createcontainerconfigerror
title: Container cannot be created because referenced config is missing (CreateContainerConfigError)
signals: [CreateContainerConfigError, "secret", "not found", "configmap", "couldn't find key", "CreateContainerError"]
mitigations: [rollback, escalate]
related: [crashloopbackoff]
---
## Symptoms
- Pod waits in `CreateContainerConfigError`; the container is never started, so there are no logs.
- Events: `Error: secret "<name>" not found`, `configmap "<name>" not found`, or `couldn't find key <key> in Secret/ConfigMap <name>`.

## Likely causes
1. **A referenced Secret does not exist** in the namespace: not created yet, created in another namespace, or deleted.
2. **A referenced ConfigMap does not exist.**
3. **The object exists but the key does not**: renamed or missing key in `secretKeyRef` / `configMapKeyRef`.

## How to confirm
- The event names the exact object and key.
- Check the pod spec for `env.valueFrom`, `envFrom` and volume references to that name.
- Was the reference introduced by the current revision, or did the object disappear from under an unchanged Deployment?

## Mitigation
- **escalate** in most cases: creating or restoring credentials is a human task, and the agent must never create a Secret with a placeholder value.
- **rollback** only if the current revision added the new reference and the previous revision ran without it.
- Never `rollout_restart`: the missing object is still missing.

## Permanent fix (needs a human)
- Create the Secret or ConfigMap with the correct values (from the team's secret store), or fix the reference name or key.
