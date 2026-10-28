# ADR 0001: Fault-injection lab as the foundation

Records why the Phase 0 lab is built the way it is.

Status: accepted, Phase 0

## Context

kubemedic's output is non-deterministic: the same cluster state can produce different diagnoses from one run to the next. To claim it works, we need a fixed, repeatable set of broken states with known answers. That is an evaluation set, the LLM equivalent of a test suite.

## Decisions

1. **Scenarios are data, not code.** Each one is plain manifests plus an `expected.yaml`. New scenarios need no Python, and the same files will drive the Phase 1 scorer.

2. **A fault must be proven before it is used.** `make check` asserts the broken state exists, and CI runs every scenario on kind. Otherwise a scenario that silently fails to break would make the agent look wrong, or right, for the wrong reason.

3. **Same-pod matching.** Assertions must all hold on one pod. During a stuck rollout, old healthy pods and new broken pods coexist. Matching any condition on any pod gives false passes.

4. **Base revision for rollback scenarios.** A rollback answer is only meaningful if a previous good revision exists, so those scenarios deploy `base.yaml` first. The validator rejects `rollback` as acceptable when there is no base.

5. **Escalation is a scored outcome.** Two scenarios have no safe in-allowlist fix. This lets us measure over-eagerness, which is the main failure mode for an agent that can change a cluster.

6. **The lab refuses non-kind contexts by default.** It deliberately breaks workloads, so a wrong `kubectl` context must not mean a broken production cluster. Overriding this takes an explicit `--allow-non-kind` flag.

7. **Lab-owned namespaces.** The lab assigns namespace placement, never the manifests, and labels each namespace. The agent's later RBAC and the executor's namespace allowlist key off the same label.

## Trade-offs

- Six scenarios is a small eval set, and accuracy on six is coarse. It is enough to build the harness. Phase 1 should add variants, such as different apps with the same fault, before reporting accuracy as a headline number.
- `kubectl` subprocess calls instead of the Python Kubernetes client: there is one less dependency and behaviour matches what a human would run. The cost is slower polling, which doesn't matter at this scale.
