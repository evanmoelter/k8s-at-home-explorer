#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/local-common.sh"
require_local_cluster
local_kubectl -n "$LOCAL_NAMESPACE" port-forward service/k8s-explorer 8000:8000
