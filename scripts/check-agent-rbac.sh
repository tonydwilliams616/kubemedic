#!/usr/bin/env bash
# Proves the agent can only read scenario workloads, and the executor can only restart or roll back Deployments, each in scenario namespaces only.
set -uo pipefail

CONTEXT="${CONTEXT:-kind-kubemedic}"
AGENT="system:serviceaccount:kubemedic-system:kubemedic-agent"
EXECUTOR="system:serviceaccount:kubemedic-system:kubemedic-executor"
AS="$AGENT"   # whose permissions check() tests; switched to the executor further down
NS="${NS:-km-bad-image-tag}"   # any scenario namespace the lab has created
failures=0

check() {  # check <yes|no> <verb> <resource[:subresource]> [namespace]
  local want=$1 verb=$2 res=$3 ns=${4:-$NS}
  local args=("$verb" "${res%%:*}")
  # "pods/log" would be read as "the pod named log", so subresources use --subresource.
  [[ $res == *:* ]] && args+=(--subresource "${res#*:}")
  local got
  got=$(kubectl --context "$CONTEXT" auth can-i "${args[@]}" -n "$ns" --as "$AS" 2>/dev/null)
  if [ "$got" = "$want" ]; then
    printf 'ok    %-3s %-7s %-22s in %s\n' "$want" "$verb" "$res" "$ns"
  else
    printf 'FAIL  want %-3s got %-3s %-7s %-22s in %s\n' "$want" "$got" "$verb" "$res" "$ns"
    failures=$((failures + 1))
  fi
}

echo "Allowed: reading workloads in $NS"
check yes get   pods
check yes list  pods
check yes get   pods:log
check yes list  events
check yes get   deployments.apps
check yes list  replicasets.apps
check yes get   configmaps
check yes list  services
check yes list  endpointslices.discovery.k8s.io

echo "Denied: secrets, writes, exec"
check no  get    secrets
check no  list   secrets
check no  delete pods
check no  create pods:exec
check no  patch  deployments.apps
check no  update deployments.apps:scale
check no  create configmaps
check no  patch  services
check no  delete services
check no  delete namespaces

echo "Denied: anything outside scenario namespaces"
check no  get  pods        kube-system
check no  get  secrets     kube-system
check no  get  pods        default
check no  list pods        kubemedic-system

AS="$EXECUTOR"
echo "Executor allowed: restart or roll back Deployments in $NS"
check yes patch  deployments.apps
check yes get    deployments.apps
check yes list   replicasets.apps

echo "Executor denied: everything else"
check no  delete deployments.apps
check no  create deployments.apps
check no  update deployments.apps:scale
check no  get    secrets
check no  create pods
check no  create pods:exec
check no  get    pods:log
check no  patch  services
check no  patch  configmaps
check no  patch  deployments.apps      kube-system
check no  patch  deployments.apps      default

if [ "$failures" -gt 0 ]; then
  echo "$failures RBAC check(s) failed"; exit 1
fi
echo "All RBAC checks passed"
