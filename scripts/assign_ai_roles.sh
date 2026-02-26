#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"
EXECUTOR_INPUT=""
SECONDARY_INPUT=""
WORKSPACE_ID="personal"

usage() {
  cat <<'EOF'
Usage: ./scripts/assign_ai_roles.sh [--executor <name>] [--secondary <name>] [--workspace <id>] [--env-file <path>]

Examples:
  ./scripts/assign_ai_roles.sh
  ./scripts/assign_ai_roles.sh --executor codex --secondary claude --workspace personal
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --executor)
      EXECUTOR_INPUT="${2:-}"
      shift
      ;;
    --secondary|--advisor)
      SECONDARY_INPUT="${2:-}"
      shift
      ;;
    --workspace)
      WORKSPACE_ID="${2:-personal}"
      shift
      ;;
    --env-file)
      ENV_FILE="${2:-$ENV_FILE}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

if [ ! -f "$ENV_FILE" ]; then
  if [ -f "${ROOT}/.env.example" ]; then
    cp "${ROOT}/.env.example" "$ENV_FILE"
  else
    touch "$ENV_FILE"
  fi
fi

sanitize_id() {
  local value="$1"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9._-' '-')"
  value="${value#-}"
  value="${value%-}"
  if [ -z "$value" ]; then
    value="ai"
  fi
  printf '%s' "$value"
}

role_consumer() {
  local input="$1"
  local role="$2"
  local base
  base="$(sanitize_id "$input")"
  if [[ "$base" == *"-${role}" ]]; then
    printf '%s' "$base"
  else
    printf '%s-%s' "$base" "$role"
  fi
}

set_env_key() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" '
    BEGIN { done=0 }
    $0 ~ ("^" k "=") { print k "=" v; done=1; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$ENV_FILE" > "$tmp"
  mv "$tmp" "$ENV_FILE"
}

choice_to_provider() {
  local choice="$1"
  local role="$2"
  case "$choice" in
    2) printf 'claude' ;;
    3) printf 'cursor' ;;
    4) printf 'chatgpt' ;;
    5)
      read -r -p "Enter custom ${role} id: " custom < /dev/tty
      printf '%s' "$custom"
      ;;
    *)
      printf 'codex'
      ;;
  esac
}

choose_provider() {
  local role="$1"
  local default_value="$2"
  local default_choice="$3"

  if [ -n "$default_value" ]; then
    printf '%s' "$default_value"
    return
  fi

  echo "Choose ${role} AI:" >&2
  echo "1) Codex" >&2
  echo "2) Claude" >&2
  echo "3) Cursor" >&2
  echo "4) ChatGPT" >&2
  echo "5) Custom" >&2
  read -r -p "Enter choice [1-5] (default ${default_choice}): " choice < /dev/tty
  if [ -z "${choice:-}" ]; then
    choice="$default_choice"
  fi
  local provider
  provider="$(choice_to_provider "$choice" "$role")"
  echo "Selected ${role}: ${provider}" >&2
  printf '%s' "$provider"
}

echo "AI role assignment"
echo
executor_choice="$(choose_provider "executor" "$EXECUTOR_INPUT" "1")"
secondary_choice="$(choose_provider "additional executor" "$SECONDARY_INPUT" "2")"
executor_consumer="$(role_consumer "$executor_choice" "executor")"
secondary_consumer="$(role_consumer "$secondary_choice" "executor")"
workspace_clean="$(sanitize_id "$WORKSPACE_ID")"

if [ "$executor_consumer" = "$secondary_consumer" ]; then
  echo "Executor and additional executor cannot be the same identity." >&2
  exit 1
fi

echo
echo "Configuration preview:"
echo "  workspace: ${workspace_clean}"
echo "  executor:  ${executor_consumer}"
echo "  additional: ${secondary_consumer}"
read -r -p "Apply this mapping? [Y/n]: " confirm
case "${confirm:-Y}" in
  n|N|no|NO)
    echo "No changes applied."
    exit 0
    ;;
esac

set_env_key "TCE_MCP_EXECUTOR_CONSUMER_ID" "$executor_consumer"
set_env_key "TCE_MCP_SECONDARY_CONSUMER_ID" "$secondary_consumer"
set_env_key "TCE_MCP_EXECUTOR_USER_ID" "$executor_consumer"
set_env_key "TCE_MCP_SECONDARY_USER_ID" "$secondary_consumer"
set_env_key "TCE_MCP_WORKSPACE_ID" "$workspace_clean"

# Keep legacy single-MCP defaults aligned to executor identity.
set_env_key "TCE_MCP_CONSUMER_ID" "$executor_consumer"
set_env_key "TCE_MCP_USER_ID" "$executor_consumer"
set_env_key "TCE_MCP_ROLE" "executor"

echo
echo "Updated AI role mapping in ${ENV_FILE}:"
echo "  executor=${executor_consumer}"
echo "  secondary=${secondary_consumer}"
echo "  workspace=${workspace_clean}"
echo
echo "Restart stack to apply:"
echo "  ./scripts/start.sh full --detach"
