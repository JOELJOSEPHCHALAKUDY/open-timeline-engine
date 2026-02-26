#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE="$ROOT/infra/docker-compose.yml"
E2E_API_PORT="${E2E_API_PORT:-18080}"
E2E_POSTGRES_PORT="${E2E_POSTGRES_PORT:-15432}"
E2E_REDIS_PORT="${E2E_REDIS_PORT:-16379}"
API="${TCE_API_BASE_URL:-http://localhost:${E2E_API_PORT}}"
TOKEN="${TCE_API_TOKEN:-local-dev-token}"

compose_cmd() {
  TCE_API_PORT="$E2E_API_PORT" TCE_POSTGRES_PORT="$E2E_POSTGRES_PORT" TCE_REDIS_PORT="$E2E_REDIS_PORT" \
    docker compose -f "$COMPOSE" "$@"
}

cleanup() {
  compose_cmd down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
fi

echo "[0/9] cleanup previous"
compose_cmd down -v >/dev/null 2>&1 || true

echo "[1/9] starting stack"
compose_cmd up -d --build \
  postgres redis tce-migrate tce-api tce-worker tce-mcp tce-mcp-secondary >/dev/null

echo "[2/9] waiting for health"
for i in $(seq 1 90); do
  if curl -fsS "$API/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
  if [ "$i" -eq 90 ]; then
    echo "health check timeout"
    exit 1
  fi
done

echo "[3/9] set timeline_only mode"
curl -fsS -X PUT "$API/v1/runtime/mode" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: e2e-user" \
  -H "X-TCE-Role: user" \
  -H "X-TCE-Workspace: personal" \
  -H "X-TCE-User: e2e-user" \
  -H "Content-Type: application/json" \
  -d '{"mode":"timeline_only"}' >/dev/null

echo "[4/9] ingest event"
NOW="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
INGEST_OUT="$(curl -fsS -X POST "$API/v1/events" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: e2e-user" \
  -H "X-TCE-Role: user" \
  -H "Content-Type: application/json" \
  -d "{
    \"schema_version\":1,
    \"ts\":\"$NOW\",
    \"actor\":\"user\",
    \"source\":\"cli\",
    \"domain\":\"coding\",
    \"task_type\":\"implement_feature\",
    \"event_type\":\"TASK_STEP\",
    \"title\":\"E2E event: implement retry policy\",
    \"payload\":{\"summary\":\"retried on 5xx\"},
    \"context\":{\"project\":\"open-timeline-engine\"},
    \"inputs\":{},
    \"steps\":[],
    \"decision\":null,
    \"outcome\":null,
    \"style\":null,
    \"links\":null,
    \"tags\":[\"e2e\"],
    \"sensitivity\":1,
    \"redaction_hints\":[]
  }")"

EVENT_ID="$(printf '%s' "$INGEST_OUT" | sed -n 's/.*"event_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
if [ -z "$EVENT_ID" ]; then
  echo "failed to parse event_id"
  echo "$INGEST_OUT"
  exit 1
fi

echo "[5/9] search"
SEARCH_OUT="$(curl -fsS -X POST "$API/v1/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: e2e-user" \
  -H "X-TCE-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"query":"retry policy","filters":{"domain":"coding"},"k":5}')"
if ! printf '%s' "$SEARCH_OUT" | grep -q '"hits"'; then
  echo "search failed"
  echo "$SEARCH_OUT"
  exit 1
fi

echo "[5.1/9] graph entity search"
GRAPH_ENTITY_OUT="$(curl -fsS "$API/v1/graph/entities?query=retry&k=10" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: e2e-user" \
  -H "X-TCE-Role: user" \
  -H "X-TCE-Workspace: personal" \
  -H "X-TCE-User: e2e-user")"
if ! printf '%s' "$GRAPH_ENTITY_OUT" | grep -q '"entities"'; then
  echo "graph entity search failed"
  echo "$GRAPH_ENTITY_OUT"
  exit 1
fi

echo "[6/9] context bundle"
BUNDLE_OUT="$(curl -fsS -X POST "$API/v1/context_bundle" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: e2e-user" \
  -H "X-TCE-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"task":"implement retry policy","app_context":{"domain":"coding"},"constraints":{"k":8}}')"
if ! printf '%s' "$BUNDLE_OUT" | grep -q '"citations"'; then
  echo "context bundle failed"
  echo "$BUNDLE_OUT"
  exit 1
fi

echo "[7/9] enable clone_advisor mode"
curl -fsS -X PUT "$API/v1/runtime/mode" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: e2e-user" \
  -H "X-TCE-Role: user" \
  -H "X-TCE-Workspace: personal" \
  -H "X-TCE-User: e2e-user" \
  -H "Content-Type: application/json" \
  -d '{"mode":"clone_advisor"}' >/dev/null

echo "[8/9] advisor guidance"
ADVICE_OUT="$(curl -fsS -X POST "$API/v1/clone/advice" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: claude-executor" \
  -H "X-TCE-Role: executor" \
  -H "Content-Type: application/json" \
  -d '{"task":"implement retry policy","app_context":{"domain":"coding"},"constraints":{"k":8},"executor_output":"I will add retries","interaction_id":"e2e-001"}')"
if ! printf '%s' "$ADVICE_OUT" | grep -q '"interaction_id"'; then
  echo "clone advice failed"
  echo "$ADVICE_OUT"
  exit 1
fi

echo "[9/9] arbitration"
ARB_OUT="$(curl -fsS -X POST "$API/v1/clone/arbitrate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: codex-executor" \
  -H "X-TCE-Role: executor" \
  -H "Content-Type: application/json" \
  -d '{"interaction_id":"e2e-001","executor_plan":"retry outbound calls","advisor_input":"also add metrics","human_override":null}')"
if ! printf '%s' "$ARB_OUT" | grep -q '"final_guidance"'; then
  echo "arbitration failed"
  echo "$ARB_OUT"
  exit 1
fi

echo "E2E PASS"
echo "api=$API"
echo "event_id=$EVENT_ID"
