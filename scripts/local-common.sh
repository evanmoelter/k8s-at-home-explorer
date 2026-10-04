#!/usr/bin/env bash
set -euo pipefail
LOCAL_CLUSTER=k8s-explorer
LOCAL_CONTEXT=kind-k8s-explorer
LOCAL_NAMESPACE=k8s-explorer
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_KUBECONFIG="$ROOT_DIR/.data/local.kubeconfig"

require_local_cluster() {
  if ! kind get clusters | grep -Fx -- "$LOCAL_CLUSTER" >/dev/null; then
    echo 'Dedicated local cluster is absent; run mise run local:up.' >&2
    exit 1
  fi
  mkdir -p "$ROOT_DIR/.data"
  kind get kubeconfig --name "$LOCAL_CLUSTER" > "$LOCAL_KUBECONFIG"
  chmod 600 "$LOCAL_KUBECONFIG"
}

local_kubectl() {
  kubectl --kubeconfig "$LOCAL_KUBECONFIG" --context "$LOCAL_CONTEXT" "$@"
}
