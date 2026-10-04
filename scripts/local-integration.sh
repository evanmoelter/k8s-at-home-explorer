#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
export EXPLORER_LOCAL_DATABASE_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export EXPLORER_LOCAL_DATABASE_PORT="${EXPLORER_LOCAL_DATABASE_PORT:-55433}"
PROJECT_NAME="k8s-explorer-test-$$"
cleanup() {
  docker compose -p "$PROJECT_NAME" down --volumes --remove-orphans >/dev/null
}
trap cleanup EXIT
docker compose -p "$PROJECT_NAME" up -d --wait postgres
export EXPLORER_TEST_DATABASE_URL="postgresql://explorer:${EXPLORER_LOCAL_DATABASE_PASSWORD}@127.0.0.1:${EXPLORER_LOCAL_DATABASE_PORT}/explorer"
uv run pytest -m integration "$@"
