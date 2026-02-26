#!/usr/bin/env bash
# self-heal.sh — rebuild changed TCE services and re-activate takeover
# Usage: self-heal.sh [--service tce-api] [--skip-takeover]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="$ROOT/infra"
API_BASE="http://localhost:${TCE_API_PORT:-8080}"
API_TOKEN="${TCE_API_TOKEN:-local-dev-token}"
SESSION_ID="${TCE_SESSION_ID:-default}"
WORKSPACE_ID="${TCE_MCP_WORKSPACE_ID:-personal}"
USER_ID="${TCE_MCP_USER_ID:-codex-executor}"

SERVICE="tce-api"
SKIP_TAKEOVER=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)  SERVICE="$2"; shift 2 ;;
    --skip-takeover) SKIP_TAKEOVER=true; shift ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

# Load .env if present
if [[ -f "$ROOT/.env" ]]; then
  set -a; . "$ROOT/.env"; set +a
fi

echo "=== TCE Self-Heal ==="

# --- Migration guard for API-related services ---
NEEDS_MIGRATE=false
case "$SERVICE" in
  tce-api|tce-worker|tce-mcp|tce-mcp-secondary|all)
    NEEDS_MIGRATE=true
    ;;
esac

cd "$COMPOSE_DIR"
if [[ "$NEEDS_MIGRATE" == "true" ]]; then
  echo "[migrate] Ensuring DB migrations are at head ..."
  docker compose up -d postgres 2>&1 | tail -5
  docker compose run --rm --build tce-migrate 2>&1 | tail -10
fi

# --- Step 1: Rebuild & restart the changed service ---
echo "[rebuild] Rebuilding $SERVICE ..."
cd "$COMPOSE_DIR"
docker compose build "$SERVICE" 2>&1 | tail -5
docker compose up -d "$SERVICE" 2>&1 | tail -5
echo "      $SERVICE rebuilt and restarted."

# --- Step 2: Wait for API health ---
echo "[health] Waiting for API health ..."
for i in $(seq 1 30); do
  if curl -sf "$API_BASE/v1/health" > /dev/null 2>&1; then
    echo "      API healthy after ${i}s."
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "      WARN: API not healthy after 30s — continuing anyway."
  fi
  sleep 1
done

# --- Step 3: Re-activate takeover ---
if [[ "$SKIP_TAKEOVER" == "false" ]]; then
  echo "[takeover] Re-activating takeover (activation_mode_default=takeover) ..."
  RESPONSE=$(curl -sf -X POST "$API_BASE/v1/takeover/step" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $API_TOKEN" \
    -H "X-TCE-Workspace: $WORKSPACE_ID" \
    -H "X-TCE-User: $USER_ID" \
    -H "X-TCE-Consumer: claude-executor" \
    -H "X-TCE-Role: executor" \
    -d "{
      \"session_id\": \"$SESSION_ID\",
      \"message\": \"self-heal completed — re-activating takeover\",
      \"activation_mode_default\": \"takeover\"
    }" 2>&1) || true

  ACTIVE=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['state']['active'])" 2>/dev/null || echo "unknown")
  echo "      Takeover active: $ACTIVE"
else
  echo "[takeover] Skipping takeover re-activation (--skip-takeover)."
fi

echo "=== Self-heal complete ==="
