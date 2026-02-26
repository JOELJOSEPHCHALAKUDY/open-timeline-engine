#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: set_mode.sh <timeline_only|clone_advisor>"
  exit 1
fi

MODE="$1"
if [ "$MODE" != "timeline_only" ] && [ "$MODE" != "clone_advisor" ]; then
  echo "invalid mode: $MODE"
  exit 1
fi

API="${TCE_API_BASE_URL:-http://localhost:8080}"
TOKEN="${TCE_API_TOKEN:-local-dev-token}"
CONSUMER="${TCE_USER_CONSUMER_ID:-${TCE_MCP_EXECUTOR_CONSUMER_ID:-cli-user}}"
ROLE="${TCE_MODE_SET_ROLE:-executor}"
WORKSPACE="${TCE_MCP_WORKSPACE_ID:-personal}"
USER_ID="${TCE_USER_ID:-$CONSUMER}"

curl -sS -X PUT \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-TCE-Consumer: $CONSUMER" \
  -H "X-TCE-Role: $ROLE" \
  -H "X-TCE-Workspace: $WORKSPACE" \
  -H "X-TCE-User: $USER_ID" \
  -d "{\"mode\":\"$MODE\"}" \
  "$API/v1/runtime/mode"

echo
