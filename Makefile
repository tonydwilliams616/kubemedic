PYTHON   ?= python3
CLUSTER  ?= kubemedic
CONTEXT  := kind-$(CLUSTER)
SCENARIO ?=
LAB      := $(PYTHON) -m lab --context $(CONTEXT)

.PHONY: help cluster-up cluster-down list break check reset break-all check-all reset-all validate test lint

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

validate: ## Validate scenario definitions (and manifests, if kubeconform is installed)
	$(PYTHON) -m lab validate
	@if command -v kubeconform >/dev/null; then \
		kubeconform -strict -summary -ignore-filename-pattern 'expected.yaml' scenarios/; \
	else echo "kubeconform not installed; skipping manifest schema check"; fi

test: ## Run unit tests
	$(PYTHON) -m pytest -q

lint: validate test ## Everything CI runs before touching a cluster
