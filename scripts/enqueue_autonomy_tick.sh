#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "${ROOT}/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "${ROOT}/.env"
  set +a
fi

API_URL="${TCE_API_BASE_URL:-http://localhost:8080}"
API_TOKEN="${TCE_API_TOKEN:-${TCE_API_TOKENS%%,*}}"
API_TOKEN="${API_TOKEN:-local-dev-token}"
WORKSPACE_ID="${TCE_MCP_WORKSPACE_ID:-personal}"
USER_ID="${TCE_MCP_USER_ID:-codex-executor}"
CONSUMER_ID="${TCE_MCP_CONSUMER_ID:-autonomy-tick-cron}"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl not found; cannot run autonomy tick." >&2
  exit 1
fi

curl -fsS -X POST "${API_URL}/v1/takeover/autonomy/tick" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "X-TCE-Workspace: ${WORKSPACE_ID}" \
  -H "X-TCE-User: ${USER_ID}" \
  -H "X-TCE-Consumer: ${CONSUMER_ID}" \
  -H "X-TCE-Role: executor" \
  -d '{"session_id":null,"include_open_discovery":true,"max_sessions":20}' >/dev/null

echo "Autonomy tick enqueued at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
