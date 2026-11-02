# Short commands (make break, make check, ...) that wrap the lab tool and kind.

# Use the project's .venv if it exists, so make always runs the Python that has our packages.
PYTHON   ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
CLUSTER  ?= kubemedic
CONTEXT  := kind-$(CLUSTER)
SCENARIO ?=
REPEATS  ?= 3
RAG      ?=
RECORDING ?= evals/recordings/nova-lite-v3-rag
# The agent's prompt version lives in the recording's meta.json, not in the folder name.
AGENT_RECORDING ?= evals/recordings/nova-lite-agent-rag
AGENT    ?=
LAB      := $(PYTHON) -m lab --context $(CONTEXT)
TF       ?= terraform
EKS_CLUSTER := kubemedic-eks
EKS_REGION  := us-east-1
EKS_CONTEXT := kubemedic-eks
SELF_HOSTED := openai:Qwen/Qwen3-8B

.PHONY: help cluster-up cluster-down list break check reset break-all check-all reset-all agent-rbac rbac-check evidence search diagnose investigate dry-run fix audit-verify eval-fixrate eval eval-retrieval record record-agent eval-replay judge-label judge-calibrate judge-cache tracking-setup mlflow-ui track-reports validate test lint tf-bootstrap eks-up eks-down gpu-test model-up model-forward model-down compare monitoring-up monitoring-forward monitoring-down grafana-password aws-running tf-check

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

agent-rbac: ## Install both identities: the agent's read-only one and the executor's narrow one
	kubectl --context $(CONTEXT) apply -f deploy/agent/rbac.yaml -f deploy/executor/rbac.yaml

rbac-check: ## Prove the agent can only read, and the executor can only restart or roll back, in scenario namespaces
	CONTEXT=$(CONTEXT) ./scripts/check-agent-rbac.sh

evidence: ## Print the agent's evidence bundle for a scenario: make evidence SCENARIO=oom-killed
	@test -n "$(SCENARIO)" || (echo "SCENARIO is required" && exit 1)
	$(PYTHON) -m kubemedic.evidence --namespace km-$(SCENARIO) --context $(CONTEXT)

search: ## Show which runbooks the agent would retrieve: make search SCENARIO=oom-killed
	@test -n "$(SCENARIO)" || (echo "SCENARIO is required" && exit 1)
	$(PYTHON) -m kubemedic.retrieve --namespace km-$(SCENARIO) --context $(CONTEXT)

diagnose: ## Ask the LLM to diagnose a scenario: make diagnose SCENARIO=oom-killed [MODEL=<id>] [RAG=1]
	@test -n "$(SCENARIO)" || (echo "SCENARIO is required" && exit 1)
	$(PYTHON) -m kubemedic.diagnose --namespace km-$(SCENARIO) --context $(CONTEXT) $(if $(MODEL),--model $(MODEL)) $(if $(RAG),--rag)

investigate: ## Let the tool-using agent investigate a scenario step by step: make investigate SCENARIO=oom-killed [MODEL=<id>] [RAG=1]
	@test -n "$(SCENARIO)" || (echo "SCENARIO is required" && exit 1)
	$(PYTHON) -m kubemedic.agent --namespace km-$(SCENARIO) --context $(CONTEXT) $(if $(MODEL),--model $(MODEL)) $(if $(RAG),--rag)

dry-run: ## Dry-run one fix as the executor (nothing changes): make dry-run SCENARIO=bad-image-tag ACTION=rollback TARGET=Deployment/web
	@test -n "$(SCENARIO)" -a -n "$(ACTION)" || (echo "SCENARIO and ACTION are required" && exit 1)
	$(PYTHON) -m kubemedic.executor --namespace km-$(SCENARIO) --context $(CONTEXT) --action $(ACTION) $(if $(TARGET),--target $(TARGET))

fix: ## Diagnose, propose and, ONLY with your typed approval, apply a fix: make fix SCENARIO=bad-image-tag [RAG=1] [AGENT=1] [MODEL=<id>]
	@test -n "$(SCENARIO)" || (echo "SCENARIO is required" && exit 1)
	$(PYTHON) -m kubemedic.fix --namespace km-$(SCENARIO) --context $(CONTEXT) $(if $(MODEL),--model $(MODEL)) $(if $(RAG),--rag) $(if $(AGENT),--agent)

audit-verify: ## Check the audit log's hash chain is intact (no record edited, removed or reordered)
	$(PYTHON) -m kubemedic.audit verify

eval-fixrate: ## End to end on the lab: break, diagnose, approve (kind-only stand-in), apply, verify, reset every scenario [RAG=1] [AGENT=1]
	$(PYTHON) -m evals.fixrate --context $(CONTEXT) $(if $(MODEL),--model $(MODEL)) $(if $(RAG),--rag) $(if $(AGENT),--agent)

eval: ## Score the diagnoser (or the agent) on every broken scenario: make eval [MODEL=<id>] [REPEATS=3] [RAG=1] [AGENT=1]
	$(PYTHON) -m evals.run --context $(CONTEXT) --repeats $(REPEATS) $(if $(MODEL),--model $(MODEL)) $(if $(RAG),--rag) $(if $(AGENT),--agent)

eval-retrieval: ## Score runbook search alone (no LLM) on every broken scenario
	$(PYTHON) -m evals.retrieval --context $(CONTEXT)

record: ## Run the real RAG eval and save every model answer for CI to replay: make record [MODEL=<id>]
	$(PYTHON) -m evals.run --context $(CONTEXT) --repeats $(REPEATS) --rag --judge $(if $(MODEL),--model $(MODEL)) --record $(RECORDING)

record-agent: ## Run the real agent eval (with runbooks) and tape both sides of every investigation for CI
	$(PYTHON) -m evals.run --context $(CONTEXT) --repeats $(REPEATS) --rag --agent --judge $(if $(MODEL),--model $(MODEL)) --record $(AGENT_RECORDING)

judge-label: ## Label the judge's calibration set yourself, one answer at a time (correct / incorrect)
	$(PYTHON) -m evals.judge --label

judge-calibrate: ## Check the LLM judge against your labels (and compare with the keyword grader)
	$(PYTHON) -m evals.judge --calibrate

judge-cache: ## Have the LLM judge grade both recordings once and cache the verdicts CI replays (needs AWS)
	$(PYTHON) -m evals.run --replay $(RECORDING) --judge
	$(PYTHON) -m evals.run --replay $(AGENT_RECORDING) --judge

# Champion/challenger: the one-shot diagnoser is the production path, so it must pass the gate.
# The agent is a challenger: its recording is replayed on every PR (a stale recording still fails)
# and its scores are reported, but they don't block a merge until it passes the gate itself.
eval-replay: ## Re-score both recordings through today's code: gate the one-shot champion, report the agent challenger (no AWS, no cluster)
	$(PYTHON) -m evals.run --replay $(RECORDING) --judge --judge-offline --gate evals/gate.yaml
	$(PYTHON) -m evals.run --replay $(AGENT_RECORDING) --judge --judge-offline

tracking-setup: ## Install MLflow (optional) so every eval run is logged to .mlflow/
	$(PYTHON) -m pip install -r requirements-tracking.txt

mlflow-ui: ## Open the MLflow dashboard at http://localhost:5000 to compare eval runs
	$(PYTHON) -m mlflow ui --backend-store-uri sqlite:///.mlflow/mlflow.db --port 5000

track-reports: ## Log report folders made before tracking existed: make track-reports [REPORTS="reports/a reports/b"]
	$(PYTHON) -m evals.tracking $(or $(REPORTS),$(wildcard reports/*))

validate: ## Validate scenario definitions (and manifests, if kubeconform is installed)
	$(PYTHON) -m lab validate
	@if command -v kubeconform >/dev/null; then \
		kubeconform -strict -summary -ignore-filename-pattern 'expected.yaml' \
			-ignore-filename-pattern 'kustomization.yaml' -ignore-filename-pattern 'deploy/monitoring/config/' \
			-schema-location default \
			-schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
			scenarios/ deploy/; \
	else echo "kubeconform not installed; skipping manifest schema check"; fi

test: ## Run unit tests
	$(PYTHON) -m pytest -q

lint: validate test ## Everything CI runs before touching a cluster

# ---- Monitoring (Phase 7): Prometheus, Pushgateway and Grafana on the kind lab ----

monitoring-up: ## Start Prometheus, Pushgateway and Grafana on kind (~0.5 GB memory) with the kubemedic dashboard
	kubectl --context $(CONTEXT) create namespace monitoring --dry-run=client -o yaml | kubectl --context $(CONTEXT) apply -f - && \
	{ kubectl --context $(CONTEXT) -n monitoring get secret grafana-admin >/dev/null 2>&1 || \
	  kubectl --context $(CONTEXT) -n monitoring create secret generic grafana-admin --from-literal=password=$$(openssl rand -hex 16); } && \
	kubectl --context $(CONTEXT) apply -k deploy/monitoring && \
	kubectl --context $(CONTEXT) -n monitoring scale deployment --all --replicas=1 && \
	for d in pushgateway prometheus grafana; do kubectl --context $(CONTEXT) -n monitoring rollout status deployment/$$d --timeout=5m || exit 1; done && \
	echo "Next: make monitoring-forward (own terminal), then in the terminal you run evals in: export KUBEMEDIC_PUSHGATEWAY=http://localhost:9091"

monitoring-forward: ## Grafana at http://localhost:3000 and the Pushgateway at http://localhost:9091 (leave running; Ctrl-C stops both)
	@kubectl --context $(CONTEXT) -n monitoring port-forward service/pushgateway 9091:9091 >/dev/null & pf=$$!; \
	trap "kill $$pf 2>/dev/null" EXIT INT TERM; \
	echo "Grafana: http://localhost:3000   Pushgateway: http://localhost:9091   (Ctrl-C to stop)"; \
	kubectl --context $(CONTEXT) -n monitoring port-forward service/grafana 3000:3000 >/dev/null

monitoring-down: ## Stop the monitoring stack to free memory; history is kept for the next monitoring-up
	kubectl --context $(CONTEXT) -n monitoring scale deployment --all --replicas=0

grafana-password: ## Print Grafana's admin password (viewing needs no login; editing does)
	@kubectl --context $(CONTEXT) -n monitoring get secret grafana-admin -o jsonpath='{.data.password}' | base64 -d; echo

# ---- AWS (Phase 6): a short-lived EKS cluster for the self-hosted model. Run `source ./aws-login.sh` first. ----

tf-bootstrap: ## ONCE: create the Terraform state bucket and a $30/month budget alert (needs TF_VAR_alert_email or asks)
	$(TF) -chdir=terraform/bootstrap init && $(TF) -chdir=terraform/bootstrap apply

eks-up: ## Create the EKS cluster with Karpenter and the GPU node pool (BILLED: ~$0.13/h until `make eks-down`; GPUs only on demand)
	@echo "This creates billable AWS resources (~\$$0.13/h). Remember: make eks-down"
	$(TF) -chdir=terraform/cluster init && \
	$(TF) -chdir=terraform/cluster apply && \
	prev=$$(kubectl config current-context 2>/dev/null || true) && \
	aws eks update-kubeconfig --name $(EKS_CLUSTER) --region $(EKS_REGION) --alias $(EKS_CONTEXT) && \
	{ [ -z "$$prev" ] || kubectl config use-context "$$prev" >/dev/null; } && \
	kubectl --context $(EKS_CONTEXT) apply -f deploy/gpu/nodepool.yaml && \
	kubectl --context $(EKS_CONTEXT) get nodes

eks-down: ## Remove GPU nodes, then destroy the cluster and its VPC (stops the hourly cost); ends with `make aws-running`
	@# Karpenter's nodes are invisible to Terraform: delete the NodePool first so Karpenter
	@# terminates them, and wait, or they would outlive the cluster (and keep billing).
	-kubectl --context $(EKS_CONTEXT) delete nodepool --all --ignore-not-found --timeout=5m
	@for i in $$(seq 1 60); do \
		n=$$(kubectl --context $(EKS_CONTEXT) get nodeclaims -o name 2>/dev/null | wc -l | tr -d ' '); \
		[ "$$n" = "0" ] && exit 0; echo "waiting for $$n Karpenter node(s) to terminate..."; sleep 10; \
	done; \
	echo "Karpenter nodes are still running: NOT destroying the cluster (they'd be orphaned). Check: kubectl --context $(EKS_CONTEXT) get nodeclaims"; exit 1
	@# The EC2NodeClass has a finalizer only the Karpenter controller can remove. Delete it now,
	@# while Karpenter is still running: once Terraform uninstalls Karpenter, it would block the
	@# CRD uninstall forever ("context deadline exceeded").
	-kubectl --context $(EKS_CONTEXT) delete ec2nodeclass --all --ignore-not-found --timeout=3m
	@# A node terminated mid-teardown can leave a VPC CNI network interface (aws-K8S-<instance>)
	@# behind, which blocks the security group and subnet. If the destroy fails, remove those and
	@# try once more.
	$(TF) -chdir=terraform/cluster destroy || { \
		echo "Destroy failed: removing network interfaces the VPC CNI left behind, then retrying once"; \
		for eni in $$(aws ec2 describe-network-interfaces --region $(EKS_REGION) \
			--filters Name=status,Values=available Name=description,Values='aws-K8S-*' \
			--query 'NetworkInterfaces[].NetworkInterfaceId' --output text); do \
			echo "deleting $$eni"; aws ec2 delete-network-interface --region $(EKS_REGION) --network-interface-id $$eni; \
		done; \
		$(TF) -chdir=terraform/cluster destroy; }
	-kubectl config delete-context $(EKS_CONTEXT) >/dev/null 2>&1
	@$(MAKE) --no-print-directory aws-running

gpu-test: ## Prove scale-from-zero and back: a pod asks for a GPU, Karpenter adds a node, nvidia-smi runs, the node goes (BILLED: ~10 min of spot GPU)
	@kubectl --context $(EKS_CONTEXT) delete pod gpu-test -n default --ignore-not-found >/dev/null
	@start=$$(date +%s); \
	kubectl --context $(EKS_CONTEXT) apply -f deploy/gpu/gpu-test.yaml && \
	echo "Waiting for Karpenter to start a GPU node and the pod to finish (usually 3-6 min)..." && \
	if ! kubectl --context $(EKS_CONTEXT) wait pod/gpu-test -n default --for=jsonpath='{.status.phase}'=Succeeded --timeout=15m; then \
		kubectl --context $(EKS_CONTEXT) describe pod gpu-test -n default | tail -20; \
		kubectl --context $(EKS_CONTEXT) get nodeclaims; exit 1; \
	fi; \
	echo "== GPU pod finished after $$(( $$(date +%s) - start ))s on:"; \
	kubectl --context $(EKS_CONTEXT) get nodes -l node-role=gpu -L node.kubernetes.io/instance-type,karpenter.sh/capacity-type,topology.kubernetes.io/zone; \
	kubectl --context $(EKS_CONTEXT) logs gpu-test -n default; \
	kubectl --context $(EKS_CONTEXT) delete pod gpu-test -n default >/dev/null; \
	echo "== Pod gone; waiting for Karpenter to remove the idle GPU node..."; \
	for i in $$(seq 1 60); do \
		[ -z "$$(kubectl --context $(EKS_CONTEXT) get nodeclaims -o name 2>/dev/null)" ] && break; sleep 10; \
	done; \
	echo "== Scaled back to zero GPU nodes, $$(( $$(date +%s) - start ))s after the request:"; \
	kubectl --context $(EKS_CONTEXT) get nodes -L node-role

model-up: ## Start the self-hosted model (vLLM + Qwen3-8B): Karpenter adds a GPU node (BILLED: spot GPU while it runs). First start ~10 min
	kubectl --context $(EKS_CONTEXT) apply -f deploy/model/vllm.yaml && \
	kubectl --context $(EKS_CONTEXT) -n model scale deployment/vllm --replicas=1 && \
	start=$$(date +%s) && \
	echo "Waiting for a GPU node, the ~16 GB download and loading onto the GPU (usually 6-12 min)..." && \
	kubectl --context $(EKS_CONTEXT) -n model rollout status deployment/vllm --timeout=25m && \
	echo "== Model ready $$(( $$(date +%s) - start ))s after the request, on:" && \
	kubectl --context $(EKS_CONTEXT) get nodes -l node-role=gpu -L node.kubernetes.io/instance-type,karpenter.sh/capacity-type && \
	echo "Next, in a second terminal: make model-forward   then: make eval MODEL=$(SELF_HOSTED) RAG=1"

model-forward: ## Make the model reachable at http://localhost:8000/v1 (leave running in its own terminal; Ctrl-C to stop)
	kubectl --context $(EKS_CONTEXT) -n model port-forward service/vllm 8000:8000

model-down: ## Stop the model: scale to zero, and Karpenter removes the GPU node about a minute later
	kubectl --context $(EKS_CONTEXT) -n model scale deployment/vllm --replicas=0 && \
	echo "Waiting for Karpenter to remove the GPU node..." && \
	for i in $$(seq 1 60); do \
		[ -z "$$(kubectl --context $(EKS_CONTEXT) get nodeclaims -o name 2>/dev/null)" ] && echo "== GPU node gone" && exit 0; sleep 10; \
	done; echo "GPU node still there after 10 min: kubectl --context $(EKS_CONTEXT) get nodeclaims"; exit 1

compare: ## Put two eval runs side by side, with cost per diagnosis and break-even: make compare A=reports/<run> B=reports/<run>
	@test -n "$(A)" -a -n "$(B)" || (echo "A and B are required: make compare A=reports/<bedrock run> B=reports/<self-hosted run>" && exit 1)
	$(PYTHON) -m evals.compare $(A) $(B)

aws-running: ## List anything in the account that bills by the hour: EKS clusters, running instances, NAT gateways, idle volumes, public IPs
	@echo "EKS clusters:";        aws eks list-clusters --region $(EKS_REGION) --query 'clusters' --output text
	@echo "Running instances:";   aws ec2 describe-instances --region $(EKS_REGION) --filters Name=instance-state-name,Values=pending,running \
		--query 'Reservations[].Instances[].[InstanceType,InstanceLifecycle,Placement.AvailabilityZone]' --output text
	@echo "NAT gateways:";        aws ec2 describe-nat-gateways --region $(EKS_REGION) --filter Name=state,Values=available --query 'NatGateways[].NatGatewayId' --output text
	@echo "Unattached volumes:";  aws ec2 describe-volumes --region $(EKS_REGION) --filters Name=status,Values=available --query 'Volumes[].[VolumeId,Size]' --output text
	@echo "Elastic IPs:";         aws ec2 describe-addresses --region $(EKS_REGION) --query 'Addresses[].PublicIp' --output text
	@echo "(empty lists = nothing billing by the hour)"

tf-check: ## Format-check and validate the Terraform (no AWS calls, no state)
	$(TF) fmt -check -recursive terraform && \
	for d in terraform/bootstrap terraform/cluster; do \
		$(TF) -chdir=$$d init -backend=false -input=false >/dev/null && $(TF) -chdir=$$d validate || exit 1; \
	done
