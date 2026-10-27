# kubemedic

An AI agent that diagnoses Kubernetes failures, retrieves the relevant runbook, and proposes a fix from a small allowlist of actions. A human approves every change, and every step is audited.

> **Status: Phase 0, fault-injection lab.** The agent itself starts in Phase 1.

## Why start with a lab

LLM agents are non-deterministic, so "it worked when I tried it" isn't evidence. Before writing any agent code, this repo builds a set of reproducible, verified failures with known root causes. That set is the evaluation suite every later phase is scored against.

## Quick start

Requires Docker, [kind](https://kind.sigs.k8s.io/) v0.24+, kubectl and Python 3.11+.

```bash
pip install -r requirements-dev.txt
make cluster-up                         # 1 control plane + 1 worker, Kubernetes 1.31
make list                               # show scenarios
make break SCENARIO=oom-killed          # inject one fault
make check SCENARIO=oom-killed          # wait until the fault is really observable
make reset SCENARIO=oom-killed          # delete its namespace
make break-all && make check-all        # the whole suite
make cluster-down
```

Cost: £0. Everything runs locally.

## Scenarios

| id | what breaks | correct outcome |
|---|---|---|
| bad-image-tag | release points at an image tag that doesn't exist | rollback |
| oom-killed | config change pushes memory past the limit | rollback |
| crashloop-bad-config-path | app looks for a config file the ConfigMap doesn't contain | rollback |
| failing-readiness-probe | new probe path returns 404, so the rollout stalls | rollback |
| missing-secret | pod references a Secret that was never created | escalate |
| unschedulable-requests | requests 64 CPU cores instead of 64m | escalate |

See [scenarios/README.md](scenarios/README.md) for the format and [ADR 0001](docs/adr/0001-fault-lab-design.md) for the design reasoning.

## Safety built in from day one

- The lab refuses to run against any non-kind kubectl context unless you pass `--allow-non-kind`.
- Scenarios cannot set their own namespace. They land in labelled `km-*` namespaces, which later scope the agent's RBAC.
- One third of the scenarios have "escalate, change nothing" as the correct answer, so over-eager fixes are measurable.

## Layout

```
kind/cluster.yaml         local cluster definition
scenarios/<id>/           base.yaml (optional), break.yaml, expected.yaml
lab/                      Python CLI: validate, break, check, reset
tests/                    unit tests for the checker and validator
.github/workflows/ci.yaml lint, unit tests, then break + check every scenario on kind
```

## Roadmap

| Phase | Scope |
|---|---|
| 0 | Fault-injection lab and verified scenarios ← *here* |
| 1 | Read-only diagnoser on Amazon Bedrock and an eval harness |
| 2 | RAG over runbooks, with citations |
| 3 | Allowlisted remediation executor, human approval and audit log |
| 4 | Alert-triggered runs and Prometheus/Grafana for agent metrics |
| 5 | EKS with Terraform, spot nodes, scale to zero |
| 6 | DR with Velero, restore behind approval |
