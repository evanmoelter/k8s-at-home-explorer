#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/local-common.sh"
require_local_cluster
cd "$ROOT_DIR"
CATALOGUE_PATH="${1:-${EXPLORER_SEED_CATALOGUE:-config/repositories.yaml}}"
if [[ ! -f "$CATALOGUE_PATH" ]]; then
  echo 'Catalogue file does not exist.' >&2
  exit 1
fi
local_kubectl -n "$LOCAL_NAMESPACE" create configmap k8s-explorer-config-seed \
  --from-file="repositories.yaml=$CATALOGUE_PATH" --dry-run=client -o yaml | local_kubectl apply -f -
local_kubectl -n "$LOCAL_NAMESPACE" patch deployment k8s-explorer --type strategic \
  --patch '{"spec":{"template":{"spec":{"volumes":[{"name":"config","configMap":{"name":"k8s-explorer-config-seed"}}]}}}}'
local_kubectl -n "$LOCAL_NAMESPACE" rollout restart deployment/k8s-explorer
local_kubectl -n "$LOCAL_NAMESPACE" rollout status deployment/k8s-explorer --timeout=180s
echo 'Pilot catalogue installed locally. Watch worker progress with:'
printf 'kubectl --kubeconfig %q --context %q -n %q logs deployment/k8s-explorer -c worker -f\n' \
  "$LOCAL_KUBECONFIG" "$LOCAL_CONTEXT" "$LOCAL_NAMESPACE"
