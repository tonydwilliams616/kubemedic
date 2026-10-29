# Runbooks

The agent's manual: short, symptom-first pages that the agent retrieves (RAG) and cites when diagnosing.

## Rules

- **One symptom per page, several causes per page.** Pages are organised the way an on-call engineer searches ("container keeps crashing"), not one page per test scenario. That keeps retrieval honest: the eval measures whether the agent can find and apply the right knowledge, not whether it can match a scenario name.
- **More pages than scenarios.** Some pages exist only so retrieval has realistic lookalikes to tell apart (for example, two pages mention exit code 137).
- **Mitigations come from the agent's menu only** (`rollback`, `rollout_restart`, `scale`, `escalate`), checked by `tests/test_runbooks.py` against `kubemedic/contract.py`.
- **Mitigation and permanent fix are separate sections.** The agent restores service; a human makes the permanent fix.
- **Short.** Under about 4,000 characters, so a page fits comfortably in the model's context.

## Format

```markdown
---
id: oomkilled                      # must match the file name
title: Container killed for exceeding its memory limit
signals: [OOMKilled, exit code 137] # strings an engineer would search for
mitigations: [rollback, escalate]   # menu actions that can apply
related: [crashloopbackoff]         # other runbook ids
---
## Symptoms
## Likely causes
## How to confirm
## Mitigation
## Permanent fix (needs a human)
```
