# Short commands (make break, make check, ...) that wrap the lab tool and kind.

# Use the project's .venv if it exists, so make always runs the Python that has our packages.
PYTHON   ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
CLUSTER  ?= kubemedic
CONTEXT  := kind-$(CLUSTER)
SCENARIO ?=
REPEATS  ?= 3
LAB      := $(PYTHON) -m lab --context $(CONTEXT)

.PHONY: help cluster-up cluster-down list break check reset break-all check-all reset-all agent-rbac rbac-check evidence search diagnose eval eval-retrieval validate test lint

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-14s %s\n",$$1,$$2}'

cluster-up: ## Create the local kind cluster
	kind create cluster --config kind/cluster.yaml --wait 120s

cluster-down: ## Delete the local kind cluster
	kind delete cluster --name $(CLUSTER)

list: ## List scenarios
	@$(PYTHON) -m lab list

break: ## Inject one fault: make break SCENARIO=oom-killed
	@test -n "$(SCENARIO)" || (echo "SCENARIO is required" && exit 1)
	$(LAB) break $(SCENARIO)

check: ## Assert the fault is observable: make check SCENARIO=oom-killed
	@test -n "$(SCENARIO)" || (echo "SCENARIO is required" && exit 1)
	$(LAB) check $(SCENARIO)

reset: ## Remove one scenario: make reset SCENARIO=oom-killed
	@test -n "$(SCENARIO)" || (echo "SCENARIO is required" && exit 1)
	$(LAB) reset $(SCENARIO)

break-all: ## Inject every fault
	$(LAB) break --all

check-all: ## Assert every fault is observable
	$(LAB) check --all

reset-all: ## Remove every scenario
	$(LAB) reset --all

agent-rbac: ## Install the agent's read-only identity (ServiceAccount + ClusterRole)
	kubectl --context $(CONTEXT) apply -f deploy/agent/rbac.yaml

rbac-check: ## Prove the agent can read scenarios but not Secrets, writes or other namespaces
	CONTEXT=$(CONTEXT) ./scripts/check-agent-rbac.sh

evidence: ## Print the agent's evidence bundle for a scenario: make evidence SCENARIO=oom-killed
	@test -n "$(SCENARIO)" || (echo "SCENARIO is required" && exit 1)
	$(PYTHON) -m kubemedic.evidence --namespace km-$(SCENARIO) --context $(CONTEXT)

search: ## Show which runbooks the agent would retrieve: make search SCENARIO=oom-killed
	@test -n "$(SCENARIO)" || (echo "SCENARIO is required" && exit 1)
	$(PYTHON) -m kubemedic.retrieve --namespace km-$(SCENARIO) --context $(CONTEXT)

diagnose: ## Ask the LLM to diagnose a scenario: make diagnose SCENARIO=oom-killed [MODEL=<bedrock model id>]
	@test -n "$(SCENARIO)" || (echo "SCENARIO is required" && exit 1)
	$(PYTHON) -m kubemedic.diagnose --namespace km-$(SCENARIO) --context $(CONTEXT) $(if $(MODEL),--model $(MODEL))

eval: ## Score the diagnoser on every broken scenario: make eval [MODEL=<id>] [REPEATS=3]
	$(PYTHON) -m evals.run --context $(CONTEXT) --repeats $(REPEATS) $(if $(MODEL),--model $(MODEL))

eval-retrieval: ## Score runbook search alone (no LLM) on every broken scenario
	$(PYTHON) -m evals.retrieval --context $(CONTEXT)

validate: ## Validate scenario definitions (and manifests, if kubeconform is installed)
	$(PYTHON) -m lab validate
	@if command -v kubeconform >/dev/null; then \
		kubeconform -strict -summary -ignore-filename-pattern 'expected.yaml' scenarios/ deploy/; \
	else echo "kubeconform not installed; skipping manifest schema check"; fi

test: ## Run unit tests
	$(PYTHON) -m pytest -q

lint: validate test ## Everything CI runs before touching a cluster
