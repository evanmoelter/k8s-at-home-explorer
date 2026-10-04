#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/local-common.sh"
cd "$ROOT_DIR"
docker info >/dev/null
docker build -t k8s-explorer:local .
mkdir -p .data
if ! kind get clusters | grep -Fx -- "$LOCAL_CLUSTER" >/dev/null; then
  kind create cluster --name "$LOCAL_CLUSTER" --kubeconfig "$LOCAL_KUBECONFIG" \
    --image kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f \
    --wait 120s
fi
require_local_cluster
kind load docker-image k8s-explorer:local --name "$LOCAL_CLUSTER"
local_kubectl apply -f deploy/local/namespace.yaml
if ! local_kubectl -n "$LOCAL_NAMESPACE" get secret k8s-explorer-database >/dev/null 2>&1; then
  python3 - <<'PY' | local_kubectl apply -f -
import json
import secrets

password = secrets.token_hex(32)
print(json.dumps({
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": "k8s-explorer-database", "namespace": "k8s-explorer"},
    "type": "Opaque",
    "stringData": {
        "password": password,
        "uri": f"postgresql://explorer:{password}@k8s-explorer-postgres:5432/explorer",
    },
}))
PY
fi
local_kubectl apply -k deploy/local
local_kubectl -n "$LOCAL_NAMESPACE" rollout status deployment/k8s-explorer-postgres --timeout=180s
local_kubectl -n "$LOCAL_NAMESPACE" rollout restart deployment/k8s-explorer
local_kubectl -n "$LOCAL_NAMESPACE" rollout status deployment/k8s-explorer --timeout=180s
echo 'Local cluster is ready. Run mise run local:forward; MCP is http://localhost:8000/mcp.'
