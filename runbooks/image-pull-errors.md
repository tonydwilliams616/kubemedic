---
id: image-pull-errors
title: Pods cannot pull their container image (ErrImagePull / ImagePullBackOff)
signals: [ErrImagePull, ImagePullBackOff, "Failed to pull image", "not found", "manifest unknown", "pull access denied", "unauthorized", "toomanyrequests"]
mitigations: [rollback, escalate]
related: [rollout-stuck]
---
## Symptoms
- Pod stays in `ErrImagePull`, then `ImagePullBackOff`; the container never starts.
- Events on the pod: `Failed to pull image "<image>": ...`.
- During a rollout the old pods usually keep serving, so this is often a stalled rollout rather than an outage.

## Likely causes
1. **The tag or digest does not exist**: a typo, or the build was tagged but never pushed. Event text: `not found` or `manifest unknown`.
2. **Registry authentication**: private registry with a missing or expired `imagePullSecret`. Event text: `pull access denied`, `unauthorized`, `401`/`403`.
3. **Registry rate limits or outage**: `toomanyrequests`, timeouts, or DNS errors reaching the registry.
4. **Wrong registry or repository name** in the image reference.

## How to confirm
- Read the exact event message; the wording separates "doesn't exist" from "not allowed" from "can't reach".
- Compare the image in the current revision with the previous one in the rollout history: did the release change it?
- If only new pods fail while old pods on the same nodes run fine, the old image is cached or valid; the new reference is the problem.

## Mitigation
- **rollback** when the failing image arrived with the current revision and the previous revision's image works. This restores the rollout immediately.
- **escalate** for authentication, rate-limit or registry outages: rolling back does not help if every revision pulls from the same broken registry, and new pods of the old revision would fail too.
- Never `rollout_restart`: new pods pull the same image and fail the same way.

## Permanent fix (needs a human)
- Correct or publish the image tag in the release pipeline; fix the `imagePullSecret`; or mirror images to a registry without rate limits.
