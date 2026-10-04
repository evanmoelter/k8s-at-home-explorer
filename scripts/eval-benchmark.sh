#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
if [[ $# -eq 0 ]]; then
  echo 'Supply corpus, judgments, mode, provider configuration, explicit provider IDs, and output.' >&2
  exit 1
fi
EXPLORER_LOCAL_DATABASE_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export EXPLORER_LOCAL_DATABASE_PASSWORD
export EXPLORER_LOCAL_DATABASE_PORT="${EXPLORER_EVAL_DATABASE_PORT:-55434}"
PROJECT_NAME="k8s-explorer-eval-$$"
cleanup() {
  docker compose -p "$PROJECT_NAME" down --volumes --remove-orphans >/dev/null
}
trap cleanup EXIT
docker compose -p "$PROJECT_NAME" up -d --wait postgres
export EXPLORER_EVAL_DATABASE_URL="postgresql://explorer:${EXPLORER_LOCAL_DATABASE_PASSWORD}@127.0.0.1:${EXPLORER_LOCAL_DATABASE_PORT}/explorer"
uv run python - <<'PY'
import os
import psycopg
with psycopg.connect(os.environ['EXPLORER_EVAL_DATABASE_URL']) as connection:
    connection.execute('CREATE EXTENSION IF NOT EXISTS vector')
PY
uv run k8s-explorer eval run --confirm-disposable-database "$@"
