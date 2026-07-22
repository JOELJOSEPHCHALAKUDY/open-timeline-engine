#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIENT="all"
API_URL="${TCE_API_BASE_URL:-http://localhost:8080}"
TOKEN="${TCE_API_TOKEN:-}"
WORKSPACE_ID="${TCE_MCP_WORKSPACE_ID:-personal}"
WORKSPACE_MODE="${TCE_MCP_WORKSPACE_MODE:-shared}"
WORKSPACE_MAP="${TCE_MCP_WORKSPACE_MAP:-}"
USER_ID="${TCE_MCP_USER_ID:-${USER:-local-user}}"
IDENTITY_MAP="${TCE_MCP_IDENTITY_MAP:-}"
CONSUMER_MAP="${TCE_MCP_CONSUMER_MAP:-}"
SESSION_MAP="${TCE_MCP_SESSION_MAP:-}"
USER_ID_EXPLICIT="false"
INSTALL_TARGETS="true"
TAKEOVER_POLICY_FILE="$ROOT/docs/mcp-config/generated/takeover_auto_call_policy.md"

usage() {
  cat <<'EOF'
Usage: ./scripts/configure_mcp_clients.sh [options]

Options:
  --client <all|claude|codex|cursor|generic|claude,codex,...>
  --api-url <url>
  --token <token>
  --workspace <workspace-id>
  --workspace-mode <shared|separate>
  --workspace-map <codex=ws-a,claude=ws-b,...>
  --user-id <user-id>  (applied only when a single client is targeted)
  --identity-map <codex=id-a,claude=id-b,...>
  --consumer-map <codex=consumer-a,claude=consumer-b,...>
  --session-map <codex=session-a,claude=session-b,...>
  --no-install      Only generate files under docs/mcp-config/generated
  -h, --help

Examples:
  ./scripts/configure_mcp_clients.sh --client all
  ./scripts/configure_mcp_clients.sh --client claude,cursor,generic
  ./scripts/configure_mcp_clients.sh --client codex,claude --identity-map codex=codex-executor,claude=claude-executor
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --client)
      CLIENT="${2:-all}"
      shift
      ;;
    --api-url)
      API_URL="${2:-$API_URL}"
      shift
      ;;
    --token)
      TOKEN="${2:-$TOKEN}"
      shift
      ;;
    --workspace)
      WORKSPACE_ID="${2:-$WORKSPACE_ID}"
      shift
      ;;
    --workspace-mode)
      WORKSPACE_MODE="${2:-$WORKSPACE_MODE}"
      shift
      ;;
    --workspace-map)
      WORKSPACE_MAP="${2:-$WORKSPACE_MAP}"
      shift
      ;;
    --user-id)
      USER_ID="${2:-$USER_ID}"
      USER_ID_EXPLICIT="true"
      shift
      ;;
    --identity-map)
      IDENTITY_MAP="${2:-$IDENTITY_MAP}"
      shift
      ;;
    --consumer-map)
      CONSUMER_MAP="${2:-$CONSUMER_MAP}"
      shift
      ;;
    --session-map)
      SESSION_MAP="${2:-$SESSION_MAP}"
      shift
      ;;
    --no-install)
      INSTALL_TARGETS="false"
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

case "$WORKSPACE_MODE" in
  shared|separate) ;;
  *)
    echo "Invalid workspace mode: ${WORKSPACE_MODE} (expected shared or separate)" >&2
    exit 1
    ;;
esac

if [ -z "$TOKEN" ] && [ -f "$ROOT/.env" ]; then
  TOKEN="$(awk -F= '$1=="TCE_API_TOKEN" {print $2}' "$ROOT/.env" | tail -n1)"
  if [ -z "$TOKEN" ]; then
    TOKEN_LIST="$(awk -F= '$1=="TCE_API_TOKENS" {print $2}' "$ROOT/.env" | tail -n1)"
    if [ -n "$TOKEN_LIST" ]; then
      TOKEN="${TOKEN_LIST%%,*}"
    fi
  fi
fi
if [ -z "$TOKEN" ]; then
  TOKEN="local-dev-token"
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required for config generation." >&2
  exit 1
fi

clients=()
if [ "$CLIENT" = "all" ]; then
  clients=("claude" "codex" "cursor" "generic")
else
  IFS=',' read -r -a parsed <<< "$CLIENT"
  for item in "${parsed[@]-}"; do
    normalized="$(printf '%s' "$item" | tr '[:upper:]' '[:lower:]' | xargs)"
    case "$normalized" in
      claude|codex|cursor|generic)
        clients+=("$normalized")
        ;;
      *)
        echo "Unsupported client: $item" >&2
        exit 1
        ;;
    esac
  done
fi
SELECTED_CLIENT_COUNT="${#clients[@]}"
if [ "$SELECTED_CLIENT_COUNT" -gt 1 ] && [ "$USER_ID_EXPLICIT" = "true" ] && [ -z "$IDENTITY_MAP" ]; then
  echo "Info: --user-id is ignored for multi-client generation; using per-client identities (<client>-executor)." >&2
fi

generated_path() {
  local client="$1"
  case "$client" in
    claude) printf '%s' "$ROOT/docs/mcp-config/generated/claude_desktop_config.json" ;;
    codex) printf '%s' "$ROOT/docs/mcp-config/generated/codex_desktop_mcp.json" ;;
    cursor) printf '%s' "$ROOT/docs/mcp-config/generated/cursor_mcp.json" ;;
    generic) printf '%s' "$ROOT/docs/mcp-config/generated/generic_mcp.json" ;;
  esac
}

target_path() {
  local client="$1"
  local os_name
  os_name="$(uname -s)"

  case "$os_name" in
    Darwin)
      case "$client" in
        claude) printf '%s' "$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
        codex) printf '%s' "${CODEX_HOME:-$HOME/.codex}/mcp.json" ;;
        cursor)
          if [ -d "$HOME/Library/Application Support/Cursor/User/globalStorage/anysphere.cursor" ]; then
            printf '%s' "$HOME/Library/Application Support/Cursor/User/globalStorage/anysphere.cursor/mcp.json"
          else
            printf '%s' "$HOME/.cursor/mcp.json"
          fi
          ;;
        generic) printf '%s' "" ;;
      esac
      ;;
    Linux)
      case "$client" in
        claude) printf '%s' "$HOME/.config/Claude/claude_desktop_config.json" ;;
        codex) printf '%s' "${CODEX_HOME:-$HOME/.codex}/mcp.json" ;;
        cursor)
          if [ -d "$HOME/.config/Cursor" ]; then
            printf '%s' "$HOME/.config/Cursor/mcp.json"
          else
            printf '%s' "$HOME/.cursor/mcp.json"
          fi
          ;;
        generic) printf '%s' "" ;;
      esac
      ;;
    *)
      if [ -n "${APPDATA:-}" ]; then
        case "$client" in
          claude) printf '%s' "$APPDATA/Claude/claude_desktop_config.json" ;;
          codex) printf '%s' "${CODEX_HOME:-$APPDATA/Codex}/mcp.json" ;;
          cursor) printf '%s' "$APPDATA/Cursor/User/globalStorage/anysphere.cursor/mcp.json" ;;
          generic) printf '%s' "" ;;
        esac
      else
        case "$client" in
          claude) printf '%s' "$HOME/.config/Claude/claude_desktop_config.json" ;;
          codex) printf '%s' "${CODEX_HOME:-$HOME/.codex}/mcp.json" ;;
          cursor) printf '%s' "$HOME/.cursor/mcp.json" ;;
          generic) printf '%s' "" ;;
        esac
      fi
      ;;
  esac
}

workspace_for_client() {
  local client="$1"
  if [ "$WORKSPACE_MODE" != "separate" ]; then
    printf '%s' "$WORKSPACE_ID"
    return
  fi

  local pair raw_key raw_value key value
  local -a _map_parts=()
  IFS=',' read -r -a _map_parts <<< "${WORKSPACE_MAP:-}"
  if [ "${#_map_parts[@]}" -gt 0 ]; then
    for pair in "${_map_parts[@]-}"; do
      raw_key="${pair%%=*}"
      raw_value="${pair#*=}"
      key="$(printf '%s' "$raw_key" | tr '[:upper:]' '[:lower:]' | xargs)"
      value="$(printf '%s' "$raw_value" | xargs)"
      if [ "$key" = "$client" ] && [ -n "$value" ]; then
        printf '%s' "$value"
        return
      fi
    done
  fi

  printf '%s' "$client"
}

lookup_map_value() {
  local map_csv="$1"
  local key="$2"
  local pair raw_key raw_value normalized_key
  local -a map_parts=()
  IFS=',' read -r -a map_parts <<< "${map_csv:-}"
  for pair in "${map_parts[@]-}"; do
    raw_key="${pair%%=*}"
    raw_value="${pair#*=}"
    normalized_key="$(printf '%s' "$raw_key" | tr '[:upper:]' '[:lower:]' | xargs)"
    if [ "$normalized_key" = "$key" ] && [ -n "$(printf '%s' "$raw_value" | xargs)" ]; then
      printf '%s' "$(printf '%s' "$raw_value" | xargs)"
      return
    fi
  done
  printf ''
}

identity_for_client() {
  local client="$1"
  local mapped
  mapped="$(lookup_map_value "$IDENTITY_MAP" "$client")"
  if [ -n "$mapped" ]; then
    printf '%s' "$mapped"
    return
  fi
  if [ "$SELECTED_CLIENT_COUNT" -eq 1 ] && [ "$USER_ID_EXPLICIT" = "true" ] && [ -n "$USER_ID" ]; then
    printf '%s' "$USER_ID"
    return
  fi
  printf '%s' "${client}-executor"
}

consumer_for_client() {
  local client="$1"
  local identity="$2"
  local mapped
  mapped="$(lookup_map_value "$CONSUMER_MAP" "$client")"
  if [ -n "$mapped" ]; then
    printf '%s' "$mapped"
    return
  fi
  printf '%s' "$identity"
}

session_for_client() {
  local client="$1"
  local mapped
  mapped="$(lookup_map_value "$SESSION_MAP" "$client")"
  if [ -n "$mapped" ]; then
    printf '%s' "$mapped"
    return
  fi
  printf '%s' "$client"
}

install_codex_via_cli() {
  local codex_workspace="$1"
  local codex_user_id="$2"
  local codex_consumer_id="$3"
  local codex_session_id="$4"
  if ! command -v codex >/dev/null 2>&1; then
    return 1
  fi

  local mcp_python
  if [ -x "$ROOT/.venv_mcp/bin/python" ]; then
    mcp_python="$ROOT/.venv_mcp/bin/python"
  else
    mcp_python="python3"
  fi
  local pythonpath
  pythonpath="${ROOT}/shared:${ROOT}/services/tce_mcp"
  codex mcp remove tce-executor >/dev/null 2>&1 || true
  codex mcp remove tce-secondary >/dev/null 2>&1 || true
  codex mcp remove tce-advisor >/dev/null 2>&1 || true

  codex mcp add tce-executor \
    --env "PYTHONPATH=${pythonpath}" \
    --env "TCE_API_BASE_URL=${API_URL}" \
    --env "TCE_API_TOKEN=${TOKEN}" \
    --env "TCE_MCP_CONSUMER_ID=${codex_consumer_id}" \
    --env "TCE_MCP_ROLE=executor" \
    --env "TCE_MCP_WORKSPACE_ID=${codex_workspace}" \
    --env "TCE_MCP_USER_ID=${codex_user_id}" \
    --env "TCE_MCP_SESSION_ID=${codex_session_id}" \
    -- "$mcp_python" -m tce_mcp.server >/dev/null
}

write_config() {
  local destination="$1"
  local client="$2"
  local workspace="$3"
  local identity_id="$4"
  local consumer_id="$5"
  local session_id="$6"
  python3 - "$destination" "$client" "$ROOT" "$API_URL" "$TOKEN" "$workspace" "$identity_id" "$consumer_id" "$session_id" <<'PY'
import json
import os
import sys
from pathlib import Path

target = Path(sys.argv[1]).expanduser()
client = sys.argv[2]
root = sys.argv[3]
api_url = sys.argv[4]
token = sys.argv[5]
workspace = sys.argv[6]
user_id = sys.argv[7]
consumer_id = sys.argv[8]
executor_session_id = sys.argv[9]

mcp_python = Path(root) / ".venv_mcp" / "bin" / "python"
if mcp_python.exists():
    command = str(mcp_python)
else:
    command = "python3"
args = ["-m", "tce_mcp.server"]
pythonpath = f'{root}/shared:{root}/services/tce_mcp'

def server() -> dict:
    return {
        "command": command,
        "args": args,
        "env": {
            "PYTHONPATH": pythonpath,
            "TCE_API_BASE_URL": api_url,
            "TCE_API_TOKEN": token,
            "TCE_MCP_CONSUMER_ID": consumer_id,
            "TCE_MCP_ROLE": "executor",
            "TCE_MCP_WORKSPACE_ID": workspace,
            "TCE_MCP_USER_ID": user_id,
            "TCE_MCP_SESSION_ID": executor_session_id,
        },
    }

snippet = {
    "mcpServers": {
        "tce-executor": server(),
    }
}

existing = {}
if target.exists():
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded
    except Exception:
        existing = {}

mcp_servers = existing.get("mcpServers")
if not isinstance(mcp_servers, dict):
    mcp_servers = {}
existing["mcpServers"] = mcp_servers
mcp_servers.update(snippet["mcpServers"])
mcp_servers.pop("tce-secondary", None)
mcp_servers.pop("tce-advisor", None)

target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

generate_takeover_policy_file() {
  mkdir -p "$(dirname "$TAKEOVER_POLICY_FILE")"
  cat > "$TAKEOVER_POLICY_FILE" <<'EOF'
# TCE takeover auto-call policy (client prompt/instructions)

Use this policy in clients that support project or custom instructions.

## Required behavior
1. Keep a stable `session_id` for the current chat.
2. On every user message, call `tce.takeover_step` with that `session_id`, user message text, and `activation_mode_default="takeover"`.
3. If `final_response` exists in the tool result, return it directly.
4. If `safety_decision` is `confirm_required`, ask for confirmation and wait.
5. Continue auto-calling `tce.takeover_step` for each later message while takeover is active.
6. Stop only when stop keyword is detected or `tce.reset_takeover_state` is called.
7. After every completed mutating change, call `tce.complete_task` unless `tce.report_execution` already captured the directive completion.

## Notes
- Activation phrases can include persona defaults (for example: "hey beru take over", "hey igris take over", "hey kurama take over").
- Stop phrases can include persona defaults (for example: "shadow stand down", "kurama stand down").
- This policy is required because MCP servers do not passively read chat text.
EOF
}

generate_takeover_policy_file

echo "Generating MCP config packs ..."
for client in "${clients[@]-}"; do
  client_workspace="$(workspace_for_client "$client")"
  client_identity="$(identity_for_client "$client")"
  client_consumer="$(consumer_for_client "$client" "$client_identity")"
  client_session="$(session_for_client "$client")"
  generated_file="$(generated_path "$client")"
  write_config "$generated_file" "$client" "$client_workspace" "$client_identity" "$client_consumer" "$client_session"
  echo "  generated: $generated_file (workspace=${client_workspace}, user=${client_identity}, session=${client_session})"

  if [ "$INSTALL_TARGETS" = "true" ]; then
    if [ "$client" = "generic" ]; then
      echo "  install skipped: generic MCP uses manual import with ${generated_file}"
      continue
    fi
    if [ "$client" = "codex" ]; then
      if install_codex_via_cli "$client_workspace" "$client_identity" "$client_consumer" "$client_session"; then
        echo "  installed: codex CLI global MCP config (~/.codex/config.toml)"
        continue
      fi
    fi
    target_file="$(target_path "$client")"
    if [ -z "$target_file" ]; then
      echo "  install skipped: no known auto-install path for client=${client}"
      continue
    fi
    write_config "$target_file" "$client" "$client_workspace" "$client_identity" "$client_consumer" "$client_session"
    echo "  installed: $target_file (workspace=${client_workspace}, user=${client_identity}, session=${client_session})"
  fi
done

echo
echo "NOTE: Generated MCP configs use absolute paths. If you move the repo, re-run:"
echo "  ./scripts/configure_mcp_clients.sh --client all"
echo
echo "MCP client configuration complete."
echo "Takeover auto-call policy: ${TAKEOVER_POLICY_FILE}"
echo "Codex/Cursor in this repo: policy is also embedded in ${ROOT}/AGENTS.md"
echo "Claude Code in this repo: policy is also embedded in ${ROOT}/CLAUDE.md"
echo "Claude Desktop app: copy the policy into Custom Instructions."
