#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/local-common.sh"
require_local_cluster
kind delete cluster --name "$LOCAL_CLUSTER"
rm -f "$LOCAL_KUBECONFIG"
