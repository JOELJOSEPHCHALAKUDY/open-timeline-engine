#!/usr/bin/env bash
set -euo pipefail

API_BASE_URL="${TCE_API_BASE_URL:-http://localhost:8080}"
API_TOKEN="${TCE_API_TOKEN:-local-dev-token}"
WORKSPACE="${TCE_TEST_WORKSPACE:-default}"
USER_ID="${TCE_TEST_USER:-default-user}"
CONSUMER="${TCE_TEST_CONSUMER:-eval-runner}"
ROLE="${TCE_TEST_ROLE:-executor}"
SESSION_ID="${1:-eval-suite}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TASKS_FILE="${2:-$REPO_ROOT/tests/eval/joel_style_tasks.json}"

if [[ ! -f "$TASKS_FILE" ]]; then
  echo "tasks file not found: $TASKS_FILE" >&2
  exit 1
fi

TASKS_JSON="$(cat "$TASKS_FILE")"
RUN_PAYLOAD="$(jq -n --arg session_id "$SESSION_ID" --argjson tasks "$TASKS_JSON" '{session_id:$session_id,tasks:$tasks,with_brief:true}')"

curl -fsS "${API_BASE_URL}/v1/retrieval/eval/run" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "X-TCE-Consumer: ${CONSUMER}" \
  -H "X-TCE-Role: ${ROLE}" \
  -H "X-TCE-Workspace: ${WORKSPACE}" \
  -H "X-TCE-User: ${USER_ID}" \
  -H "Content-Type: application/json" \
  -d "${RUN_PAYLOAD}"

echo
curl -fsS "${API_BASE_URL}/v1/retrieval/eval/status?session_id=${SESSION_ID}" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "X-TCE-Consumer: ${CONSUMER}" \
  -H "X-TCE-Role: ${ROLE}" \
  -H "X-TCE-Workspace: ${WORKSPACE}" \
  -H "X-TCE-User: ${USER_ID}"
echo

