#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE="$ROOT/infra/docker-compose.lite.yml"
E2E_API_PORT="${E2E_API_PORT:-18081}"
API="${TCE_API_BASE_URL:-http://localhost:${E2E_API_PORT}}"
TOKEN="${TCE_API_TOKEN:-local-dev-token}"

compose_cmd() {
  TCE_API_PORT="$E2E_API_PORT" docker compose -f "$COMPOSE" "$@"
}

cleanup() {
  compose_cmd down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
fi

echo "[0/8] cleanup previous"
compose_cmd down -v >/dev/null 2>&1 || true

echo "[1/8] starting lite stack"
compose_cmd up -d --build tce-lite-api tce-mcp tce-mcp-secondary >/dev/null

echo "[2/8] waiting for health"
for i in $(seq 1 60); do
  if curl -fsS "$API/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
  if [ "$i" -eq 60 ]; then
    echo "health check timeout"
    exit 1
  fi
done

echo "[3/8] ingest event"
NOW="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
INGEST_OUT="$(curl -fsS -X POST "$API/v1/events" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: lite-e2e-user" \
  -H "X-TCE-Role: user" \
  -H "Content-Type: application/json" \
  -d "{
    \"schema_version\":1,
    \"ts\":\"$NOW\",
    \"actor\":\"user\",
    \"source\":\"cli\",
    \"domain\":\"coding\",
    \"task_type\":\"debug\",
    \"event_type\":\"TASK_STEP\",
    \"title\":\"Lite E2E: investigate timeout retry\",
    \"payload\":{\"summary\":\"investigated retry strategy\"},
    \"context\":{\"project\":\"open-timeline-engine\"},
    \"inputs\":{},
    \"steps\":[],
    \"decision\":null,
    \"outcome\":null,
    \"style\":null,
    \"links\":null,
    \"tags\":[\"e2e\",\"lite\"],
    \"sensitivity\":1,
    \"redaction_hints\":[]
  }")"

EVENT_ID="$(printf '%s' "$INGEST_OUT" | sed -n 's/.*"event_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
if [ -z "$EVENT_ID" ]; then
  echo "failed to parse event_id"
  echo "$INGEST_OUT"
  exit 1
fi

echo "[4/8] search"
SEARCH_OUT="$(curl -fsS -X POST "$API/v1/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: lite-e2e-user" \
  -H "X-TCE-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"query":"retry timeout","filters":{"domain":"coding"},"k":5}')"
if ! printf '%s' "$SEARCH_OUT" | grep -q '"hits"'; then
  echo "search failed"
  echo "$SEARCH_OUT"
  exit 1
fi

echo "[4.1/8] graph entity search"
GRAPH_ENTITY_OUT="$(curl -fsS "$API/v1/graph/entities?query=retry&k=10" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: lite-e2e-user" \
  -H "X-TCE-Role: user" \
  -H "X-TCE-Workspace: personal" \
  -H "X-TCE-User: lite-e2e-user")"
if ! printf '%s' "$GRAPH_ENTITY_OUT" | grep -q '"entities"'; then
  echo "graph entity search failed"
  echo "$GRAPH_ENTITY_OUT"
  exit 1
fi

echo "[5/8] context bundle"
BUNDLE_OUT="$(curl -fsS -X POST "$API/v1/context_bundle" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: lite-e2e-user" \
  -H "X-TCE-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"task":"fix timeout retry behavior","app_context":{"domain":"coding"},"constraints":{"k":8}}')"
if ! printf '%s' "$BUNDLE_OUT" | grep -q '"citations"'; then
  echo "context bundle failed"
  echo "$BUNDLE_OUT"
  exit 1
fi

echo "[6/8] clone advice fallback in timeline mode"
ADVICE_FALLBACK_OUT="$(curl -fsS -X POST "$API/v1/clone/advice" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: claude-executor" \
  -H "X-TCE-Role: executor" \
  -H "Content-Type: application/json" \
  -d '{"task":"propose retry plan","app_context":{"domain":"coding"},"constraints":{"k":8},"interaction_id":"lite-e2e-001"}')"
if ! printf '%s' "$ADVICE_FALLBACK_OUT" | grep -q '"interaction_id"'; then
  echo "clone fallback advice failed"
  echo "$ADVICE_FALLBACK_OUT"
  exit 1
fi

echo "[7/8] enable clone_advisor and arbitrate"
curl -fsS -X PUT "$API/v1/runtime/mode" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: lite-e2e-user" \
  -H "X-TCE-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"mode":"clone_advisor"}' >/dev/null

ARB_OUT="$(curl -fsS -X POST "$API/v1/clone/arbitrate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-TCE-Consumer: codex-executor" \
  -H "X-TCE-Role: executor" \
  -H "Content-Type: application/json" \
  -d '{"interaction_id":"lite-e2e-001","executor_plan":"retry outbound calls","advisor_input":"also add retry metrics","human_override":null}')"
if ! printf '%s' "$ARB_OUT" | grep -q '"final_guidance"'; then
  echo "clone arbitration failed"
  echo "$ARB_OUT"
  exit 1
fi

echo "[8/8] complete"
echo "LITE E2E PASS"
echo "api=$API"
echo "event_id=$EVENT_ID"
