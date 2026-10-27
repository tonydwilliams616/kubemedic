#!/usr/bin/env bash
# Proves the agent's identity can read scenario workloads but cannot read Secrets, change anything, or see other namespaces.
set -uo pipefail

CONTEXT="${CONTEXT:-kind-kubemedic}"
AGENT="system:serviceaccount:kubemedic-system:kubemedic-agent"
NS="${NS:-km-bad-image-tag}"   # any scenario namespace the lab has created
failures=0

check() {  # check <yes|no> <verb> <resource[:subresource]> [namespace]
  local want=$1 verb=$2 res=$3 ns=${4:-$NS}
  local args=("$verb" "${res%%:*}")
  # "pods/log" would be read as "the pod named log", so subresources use --subresource.
  [[ $res == *:* ]] && args+=(--subresource "${res#*:}")
  local got
  got=$(kubectl --context "$CONTEXT" auth can-i "${args[@]}" -n "$ns" --as "$AGENT" 2>/dev/null)
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

echo "Denied: secrets, writes, exec"
check no  get    secrets
check no  list   secrets
check no  delete pods
check no  create pods:exec
check no  patch  deployments.apps
check no  update deployments.apps:scale
check no  create configmaps
check no  delete namespaces

echo "Denied: anything outside scenario namespaces"
check no  get  pods        kube-system
check no  get  secrets     kube-system
check no  get  pods        default
check no  list pods        kubemedic-system

if [ "$failures" -gt 0 ]; then
  echo "$failures RBAC check(s) failed"; exit 1
fi
echo "All RBAC checks passed"
