#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"
LOCAL_CAPTURE_VENV="${ROOT}/.venv_cli_capture"
LOCAL_CAPTURE_LAUNCHER="${ROOT}/scripts/tce-capture"
LOCAL_MCP_VENV="${ROOT}/.venv_mcp"
LOCAL_MCP_LAUNCHER="${ROOT}/scripts/tce-mcp"

INSTALL_LOG="${ROOT}/install.log"
exec > >(tee "$INSTALL_LOG") 2>&1
echo "Install log: ${INSTALL_LOG}"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

STACK_MODE="full"
STACK_SET_BY_ARG="false"
RUNTIME_PROFILE=""
RUNTIME_PROFILE_SET_BY_ARG="false"
ENV_CREATED_THIS_RUN="false"
OPERATION_MODE="timeline_only"
OPERATION_SET_BY_ARG="false"
SETUP_MODE="setup"
SETUP_MODE_SET_BY_ARG="false"
PERSONA_MODE="normal"
REAL_TAKEOVER="false"
REAL_TAKEOVER_MODE="suggest"
ADVISOR_COMMAND=""
ADVISOR_TIMEOUT_SECONDS="45"
ADVISOR_TCE_ENRICH="true"
ADVISOR_ACTIVATION_KEYWORD=""
ADVISOR_STOP_KEYWORD=""
ADVISOR_PRIMARY_PROVIDER="openai"
ADVISOR_PRIMARY_MODEL="gpt-4o-mini"
ADVISOR_FALLBACK_CHAIN="deepseek,local_ollama"
ADVISOR_CUSTOM_BASE_URL=""
ADVISOR_CUSTOM_MODEL=""
ADVISOR_CUSTOM_API_KEY_REF="advisor-custom"
ADVISOR_PROVIDER_TIMEOUT_MS="6000"
ADVISOR_PROVIDER_RETRY_MAX="2"
ADVISOR_PROVIDER_API_KEY=""
ADVISOR_FALLBACK_MODEL_HINTS=""
ADVISOR_ENV_KEY_OVERRIDES=""
EXECUTOR_CLIENTS="codex,claude,cursor"
WORKSPACE_MEMORY_MODE="shared"
WORKSPACE_MAP=""
EXECUTOR_USER_ID=""
EXECUTOR_CONSUMER_ID=""
EXECUTOR_SESSION_ID=""
ADVISOR_USER_ID=""
ADVISOR_CONSUMER_ID=""
ADVISOR_SESSION_ID=""
LOVABLE_SELECTED="false"
ACTION="install"
ASSUME_YES="false"
AUTO_CAPTURE_INTERACTIONS="true"
AUTO_CAPTURE_SKIP_SENSITIVE="true"
AUTO_CAPTURE_MAX_CHARS="2000"
CLONE_MAX_TURNS_PER_INTERACTION="100"
PERSONA_COMMAND_STYLE="default"
TAKEOVER_SAFETY_POLICY="high-risk-pause"
TAKEOVER_CONFIRM_KEYWORD="confirm"
TAKEOVER_DENY_KEYWORD="abort"
RUN_TAKEOVER_SELF_TEST="false"
REMOVE_WORKSPACE_POLICY="false"
RESTORE_BACKUP_FILE=""
TAKEOVER_POLICY_START_MARKER="# >>> TCE TAKEOVER POLICY >>>"
TAKEOVER_POLICY_END_MARKER="# <<< TCE TAKEOVER POLICY <<<"
MAINTENANCE_CRON_MARKER="# open-timeline-engine-maintenance"
MAINTENANCE_CRON_DEFAULT_EXPR="17 3 * * 0"
AUTONOMY_TICK_CRON_MARKER="# open-timeline-engine-autonomy-tick"
AUTONOMY_TICK_CRON_DEFAULT_EXPR="*/5 * * * *"
BACKUP_CRON_MARKER="# open-timeline-engine-backup"
BACKUP_CRON_DEFAULT_EXPR="30 2 * * *"
BACKUP_RETENTION_DAYS_DEFAULT="14"

usage() {
  cat <<'EOF'
Usage: ./scripts/install.sh [install|restart|stop|remove|doctor|fix|backup|restore] [full|lite|all|auto] [--yes]
       ./scripts/install.sh [install|restart|stop|remove|doctor|fix|backup|restore] [full|lite|all|auto] [--profile local-lite|local-full|team-secure|research] [--behavior timeline_only|clone_advisor] [--setup-mode setup|env] [--yes] [--remove-policy] [--backup-file <path>]

Examples:
  ./scripts/install.sh
  ./scripts/install.sh install full
  ./scripts/install.sh install full --profile local-full
  ./scripts/install.sh install full --profile team-secure
  ./scripts/install.sh restart full
  ./scripts/install.sh install full --behavior clone_advisor --yes
  ./scripts/install.sh install full --setup-mode env --yes
  ./scripts/install.sh stop lite
  ./scripts/install.sh remove all --yes
  ./scripts/install.sh remove all --yes --remove-policy
  ./scripts/install.sh doctor auto
  ./scripts/install.sh fix auto
  ./scripts/install.sh backup
  ./scripts/install.sh restore --backup-file backups/manual/tce_20260220_010203.sql.gz
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    install|start)
      ACTION="install"
      ;;
    restart|rebuild)
      ACTION="restart"
      ;;
    stop)
      ACTION="stop"
      ;;
    remove|uninstall|purge)
      ACTION="remove"
      ;;
    doctor)
      ACTION="doctor"
      ;;
    fix|repair|heal)
      ACTION="fix"
      ;;
    backup)
      ACTION="backup"
      ;;
    restore)
      ACTION="restore"
      ;;
    full|lite|all|auto)
      STACK_MODE="$1"
      STACK_SET_BY_ARG="true"
      ;;
    -y|--yes)
      ASSUME_YES="true"
      ;;
    --profile)
      RUNTIME_PROFILE="${2:-}"
      case "$RUNTIME_PROFILE" in
        local-lite|local-full|team-secure|research)
          ;;
        *)
          echo "Invalid runtime profile: ${RUNTIME_PROFILE:-<empty>}" >&2
          exit 1
          ;;
      esac
      RUNTIME_PROFILE_SET_BY_ARG="true"
      shift
      ;;
    --remove-policy)
      REMOVE_WORKSPACE_POLICY="true"
      ;;
    --backup-file)
      RESTORE_BACKUP_FILE="${2:-}"
      if [ -z "$RESTORE_BACKUP_FILE" ]; then
        echo "--backup-file requires a value." >&2
        exit 1
      fi
      shift
      ;;
    --behavior|--operation-mode)
      behavior_value="${2:-}"
      case "$behavior_value" in
        timeline_only|timeline)
          OPERATION_MODE="timeline_only"
          ;;
        clone_advisor|clone|advisor)
          OPERATION_MODE="clone_advisor"
          ;;
        *)
          echo "Invalid behavior: ${behavior_value:-<empty>} (expected timeline_only or clone_advisor)" >&2
          exit 1
          ;;
      esac
      OPERATION_SET_BY_ARG="true"
      shift
      ;;
    --setup-mode)
      setup_mode_value="${2:-}"
      case "$setup_mode_value" in
        setup|wizard)
          SETUP_MODE="setup"
          ;;
        env|existing_env|existing)
          SETUP_MODE="env"
          ;;
        *)
          echo "Invalid setup mode: ${setup_mode_value:-<empty>} (expected setup or env)" >&2
          exit 1
          ;;
      esac
      SETUP_MODE_SET_BY_ARG="true"
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

backup_env() {
  if [ -n "${ENV_BACKUP:-}" ]; then
    return
  fi
  if [ -f "$ENV_FILE" ]; then
    local backup="${ENV_FILE}.backup.$(date +%s)"
    cp "$ENV_FILE" "$backup"
    ENV_BACKUP="$backup"
    echo "Backed up .env to ${backup}"
  fi
}

restore_env() {
  if [ -n "${ENV_BACKUP:-}" ] && [ -f "$ENV_BACKUP" ]; then
    cp "$ENV_BACKUP" "$ENV_FILE"
    echo "Restored .env from backup: ${ENV_BACKUP}"
  fi
}

cleanup_env_backups() {
  local keep=3
  local backups
  backups="$(ls -1t "${ENV_FILE}.backup."* 2>/dev/null || true)"
  if [ -z "$backups" ]; then
    return
  fi
  echo "$backups" | tail -n +$((keep + 1)) | while IFS= read -r old; do
    rm -f "$old"
  done
}

ENV_BACKUP=""

env_value_or_default() {
  local key="$1"
  local fallback="$2"
  local value=""
  if [ -f "$ENV_FILE" ]; then
    value="$(awk -F= -v k="$key" '$1==k {print $2}' "$ENV_FILE" | tail -n 1)"
  fi
  if [ -n "${value:-}" ]; then
    printf '%s' "$value"
  else
    printf '%s' "$fallback"
  fi
}

first_csv_token() {
  local csv="$1"
  local first="${csv%%,*}"
  first="${first#"${first%%[![:space:]]*}"}"
  first="${first%"${first##*[![:space:]]}"}"
  printf '%s' "$first"
}

second_csv_token() {
  local csv="$1"
  local rest="${csv#*,}"
  if [ "$rest" = "$csv" ]; then
    printf ''
    return
  fi
  local second="${rest%%,*}"
  second="${second#"${second%%[![:space:]]*}"}"
  second="${second%"${second##*[![:space:]]}"}"
  printf '%s' "$second"
}

csv_to_json_array() {
  local csv="$1"
  local out="["
  local token
  IFS=',' read -r -a _parts <<< "$csv"
  for token in "${_parts[@]-}"; do
    token="${token#"${token%%[![:space:]]*}"}"
    token="${token%"${token##*[![:space:]]}"}"
    if [ -z "$token" ]; then
      continue
    fi
    if [ "$out" != "[" ]; then
      out+=","
    fi
    out+="\"$token\""
  done
  out+="]"
  printf '%s' "$out"
}

build_advisor_routes_json() {
  local primary_provider="$1"
  local primary_model="$2"
  local primary_base_url="$3"
  local primary_api_key_ref="$4"
  local fallback_csv="$5"
  local fallback_model_hints="$6"
  python3 - "$primary_provider" "$primary_model" "$primary_base_url" "$primary_api_key_ref" "$fallback_csv" "$fallback_model_hints" <<'PY'
import json
import sys

primary_provider, primary_model, primary_base_url, primary_api_key_ref, fallback_csv, fallback_hints = sys.argv[1:7]

hint_map = {}
for token in (fallback_hints or "").split(","):
    token = token.strip()
    if not token or ":" not in token:
        continue
    provider, model = token.split(":", 1)
    provider = provider.strip().lower()
    model = model.strip()
    if provider and model:
        hint_map[provider] = model

routes = []
routes.append(
    {
        "provider_id": primary_provider.strip().lower(),
        "model": (primary_model or "").strip() or None,
        "api_key_ref": (primary_api_key_ref or "").strip() or None,
        "base_url": (primary_base_url or "").strip() or None,
        "api_version": None,
        "region_hint": None,
        "priority": 0,
    }
)

priority = 1
seen = {str(routes[0]["provider_id"])}
for raw in (fallback_csv or "").split(","):
    provider_id = raw.strip().lower()
    if not provider_id or provider_id in seen:
        continue
    seen.add(provider_id)
    key_ref = None if provider_id.startswith("local_") else f"advisor-{provider_id}"
    routes.append(
        {
            "provider_id": provider_id,
            "model": hint_map.get(provider_id),
            "api_key_ref": key_ref,
            "base_url": None,
            "api_version": None,
            "region_hint": None,
            "priority": priority,
        }
    )
    priority += 1

print(json.dumps(routes, separators=(",", ":")))
PY
}

build_workspace_map_for_clients() {
  local workspace_base="$1"
  local clients_csv="$2"
  local token normalized out=""
  IFS=',' read -r -a _parts <<< "$clients_csv"
  for token in "${_parts[@]-}"; do
    normalized="$(printf '%s' "$token" | tr '[:upper:]' '[:lower:]' | xargs)"
    [ -z "$normalized" ] && continue
    [ -z "$out" ] && out="${normalized}=${normalized}" && continue
    out="${out},${normalized}=${normalized}"
  done
  printf '%s' "$out"
}

build_identity_map_for_clients() {
  local clients_csv="$1"
  local primary_identity="${2:-}"
  local secondary_identity="${3:-}"
  local token normalized out="" idx=0 identity=""
  IFS=',' read -r -a _parts <<< "$clients_csv"
  for token in "${_parts[@]-}"; do
    normalized="$(printf '%s' "$token" | tr '[:upper:]' '[:lower:]' | xargs)"
    [ -z "$normalized" ] && continue
    identity=""
    if [ "$idx" -eq 0 ] && [ -n "$primary_identity" ]; then
      identity="$primary_identity"
    elif [ "$idx" -eq 1 ] && [ -n "$secondary_identity" ]; then
      identity="$secondary_identity"
    else
      identity="${normalized}-executor"
    fi
    [ -z "$out" ] && out="${normalized}=${identity}" || out="${out},${normalized}=${identity}"
    idx=$((idx + 1))
  done
  printf '%s' "$out"
}

build_session_map_for_clients() {
  local clients_csv="$1"
  local primary_session="${2:-}"
  local secondary_session="${3:-}"
  local token normalized out="" idx=0 session=""
  IFS=',' read -r -a _parts <<< "$clients_csv"
  for token in "${_parts[@]-}"; do
    normalized="$(printf '%s' "$token" | tr '[:upper:]' '[:lower:]' | xargs)"
    [ -z "$normalized" ] && continue
    session=""
    if [ "$idx" -eq 0 ] && [ -n "$primary_session" ]; then
      session="$primary_session"
    elif [ "$idx" -eq 1 ] && [ -n "$secondary_session" ]; then
      session="$secondary_session"
    else
      session="$normalized"
    fi
    [ -z "$out" ] && out="${normalized}=${session}" || out="${out},${normalized}=${session}"
    idx=$((idx + 1))
  done
  printf '%s' "$out"
}

stack_requires_full_qdrant() {
  case "$1" in
    full|all|auto)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

ensure_qdrant_ready_for_stack() {
  local stack_target="$1"
  if ! stack_requires_full_qdrant "$stack_target"; then
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "Warning: docker not found; skipping qdrant readiness check."
    return 0
  fi
  if ! docker compose version >/dev/null 2>&1; then
    echo "Warning: docker compose not available; skipping qdrant readiness check."
    return 0
  fi

  docker compose -f "${ROOT}/infra/docker-compose.yml" up -d qdrant >/dev/null 2>&1 || true

  local qdrant_port
  qdrant_port="$(env_value_or_default "TCE_QDRANT_PORT" "6333")"

  if command -v curl >/dev/null 2>&1; then
    for _ in $(seq 1 40); do
      if curl -fsS "http://localhost:${qdrant_port}/collections" >/dev/null 2>&1; then
        echo "Qdrant status: ready"
        return 0
      fi
      sleep 1
    done
    echo "Warning: Qdrant health check did not pass; attempting one restart."
    docker compose -f "${ROOT}/infra/docker-compose.yml" restart qdrant >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do
      if curl -fsS "http://localhost:${qdrant_port}/collections" >/dev/null 2>&1; then
        echo "Qdrant status: recovered after restart"
        return 0
      fi
      sleep 1
    done
    echo "Warning: Qdrant is still not reachable on port ${qdrant_port}."
    return 0
  fi

  if docker compose -f "${ROOT}/infra/docker-compose.yml" ps --services --filter status=running 2>/dev/null | grep -qx "qdrant"; then
    echo "Qdrant status: running"
  else
    echo "Warning: Qdrant service is not running."
  fi
  return 0
}

provider_display_name() {
  case "$1" in
    local_ollama) printf '%s' "Local Ollama" ;;
    openai) printf '%s' "OpenAI" ;;
    anthropic) printf '%s' "Anthropic" ;;
    gemini) printf '%s' "Gemini" ;;
    openrouter) printf '%s' "OpenRouter" ;;
    groq) printf '%s' "Groq" ;;
    together) printf '%s' "Together" ;;
    xai) printf '%s' "xAI" ;;
    deepseek) printf '%s' "DeepSeek" ;;
    dashscope) printf '%s' "DashScope/Qwen" ;;
    zhipu) printf '%s' "Zhipu GLM" ;;
    moonshot) printf '%s' "Moonshot Kimi" ;;
    qianfan) printf '%s' "Baidu Qianfan/ERNIE" ;;
    hunyuan) printf '%s' "Tencent Hunyuan" ;;
    custom) printf '%s' "Custom OpenAI-compatible" ;;
    *) printf '%s' "$1" ;;
  esac
}

provider_category() {
  case "$1" in
    deepseek|dashscope|zhipu|moonshot|qianfan|hunyuan) printf '%s' "china" ;;
    custom|local_ollama) printf '%s' "custom" ;;
    *) printf '%s' "global" ;;
  esac
}

ensure_fallback_category_coverage() {
  local primary_provider="$1"
  local fallback_csv="$2"
  local defaults_global="openai"
  local defaults_china="deepseek"
  local defaults_custom="local_ollama"
  local has_global="false"
  local has_china="false"
  local has_custom="false"
  local token normalized out=""

  case "$(provider_category "$primary_provider")" in
    global) has_global="true" ;;
    china) has_china="true" ;;
    custom) has_custom="true" ;;
  esac

  IFS=',' read -r -a _parts <<< "$fallback_csv"
  for token in "${_parts[@]-}"; do
    normalized="$(printf '%s' "$token" | tr '[:upper:]' '[:lower:]' | xargs)"
    [ -z "$normalized" ] && continue
    [ "$normalized" = "$primary_provider" ] && continue
    case "$(provider_category "$normalized")" in
      global) has_global="true" ;;
      china) has_china="true" ;;
      custom) has_custom="true" ;;
    esac
    if [ -z "$out" ]; then
      out="$normalized"
    elif ! printf ',%s,' "$out" | grep -q ",${normalized},"; then
      out="${out},${normalized}"
    fi
  done

  if [ "$has_global" != "true" ]; then
    out="${out:+${out},}${defaults_global}"
  fi
  if [ "$has_china" != "true" ]; then
    out="${out:+${out},}${defaults_china}"
  fi
  if [ "$has_custom" != "true" ]; then
    out="${out:+${out},}${defaults_custom}"
  fi

  # remove accidental primary duplicates after defaults append
  local final_out=""
  IFS=',' read -r -a _final_parts <<< "$out"
  for token in "${_final_parts[@]-}"; do
    normalized="$(printf '%s' "$token" | tr '[:upper:]' '[:lower:]' | xargs)"
    [ -z "$normalized" ] && continue
    [ "$normalized" = "$primary_provider" ] && continue
    if [ -z "$final_out" ]; then
      final_out="$normalized"
    elif ! printf ',%s,' "$final_out" | grep -q ",${normalized},"; then
      final_out="${final_out},${normalized}"
    fi
  done
  printf '%s' "$final_out"
}

provider_default_model() {
  case "$1" in
    local_ollama) printf '%s' "qwen2.5-coder:7b" ;;
    openai) printf '%s' "gpt-4o-mini" ;;
    anthropic) printf '%s' "claude-3-5-sonnet-latest" ;;
    gemini) printf '%s' "gemini-2.0-flash" ;;
    openrouter) printf '%s' "openai/gpt-4o-mini" ;;
    groq) printf '%s' "llama-3.3-70b-versatile" ;;
    together) printf '%s' "meta-llama/Llama-3.3-70B-Instruct-Turbo" ;;
    xai) printf '%s' "grok-2-latest" ;;
    deepseek) printf '%s' "deepseek-chat" ;;
    dashscope) printf '%s' "qwen-plus" ;;
    zhipu) printf '%s' "glm-4-plus" ;;
    moonshot) printf '%s' "moonshot-v1-8k" ;;
    qianfan) printf '%s' "ernie-4.0-8k" ;;
    hunyuan) printf '%s' "hunyuan-turbo" ;;
    custom) printf '%s' "" ;;
    *) printf '%s' "" ;;
  esac
}

provider_api_env_key() {
  case "$1" in
    openai) printf '%s' "TCE_OPENAI_API_KEY" ;;
    anthropic) printf '%s' "TCE_ANTHROPIC_API_KEY" ;;
    gemini) printf '%s' "TCE_GEMINI_API_KEY" ;;
    openrouter) printf '%s' "TCE_OPENROUTER_API_KEY" ;;
    groq) printf '%s' "TCE_GROQ_API_KEY" ;;
    together) printf '%s' "TCE_TOGETHER_API_KEY" ;;
    xai) printf '%s' "TCE_XAI_API_KEY" ;;
    deepseek) printf '%s' "TCE_DEEPSEEK_API_KEY" ;;
    dashscope) printf '%s' "TCE_DASHSCOPE_API_KEY" ;;
    zhipu) printf '%s' "TCE_ZHIPU_API_KEY" ;;
    moonshot) printf '%s' "TCE_MOONSHOT_API_KEY" ;;
    qianfan) printf '%s' "TCE_QIANFAN_API_KEY" ;;
    hunyuan) printf '%s' "TCE_HUNYUAN_API_KEY" ;;
    custom) printf '%s' "TCE_ADVISOR_CUSTOM_API_KEY" ;;
    *) printf '%s' "" ;;
  esac
}

provider_api_key_ref() {
  local provider="$1"
  case "$provider" in
    ""|local_ollama|local_lmstudio)
      printf '%s' ""
      ;;
    *)
      printf 'advisor-%s' "$provider"
      ;;
  esac
}

default_local_ollama_base_url() {
  if [ "$STACK_MODE" = "full" ]; then
    printf '%s' "http://ollama:11434"
  else
    printf '%s' "http://localhost:11434"
  fi
}

default_local_lmstudio_base_url() {
  if [ "$STACK_MODE" = "full" ]; then
    printf '%s' "http://host.docker.internal:1234/v1"
  else
    printf '%s' "http://localhost:1234/v1"
  fi
}

append_env_override() {
  local key="$1"
  local value="$2"
  if [ -z "$key" ] || [ -z "$value" ]; then
    return
  fi
  ADVISOR_ENV_KEY_OVERRIDES="${ADVISOR_ENV_KEY_OVERRIDES}${key}=${value}"$'\n'
}

normalize_executor_clients() {
  local raw="$1"
  local normalized=""
  local token
  LOVABLE_SELECTED="false"
  IFS=',' read -r -a _parts <<< "$raw"
  for token in "${_parts[@]-}"; do
    token="$(printf '%s' "$token" | tr '[:upper:]' '[:lower:]' | xargs)"
    case "$token" in
      codex|claude|cursor|generic)
        if [[ ",${normalized}," != *",${token},"* ]]; then
          normalized="${normalized:+${normalized},}${token}"
        fi
        ;;
      lovable|lovable-ai|lovableai)
        LOVABLE_SELECTED="true"
        if [[ ",${normalized}," != *",generic,"* ]]; then
          normalized="${normalized:+${normalized},}generic"
        fi
        ;;
      "")
        ;;
      *)
        echo "Warning: unsupported executor client '${token}' ignored."
        ;;
    esac
  done
  if [ -z "$normalized" ]; then
    normalized="codex,claude,cursor"
  fi
  EXECUTOR_CLIENTS="$normalized"
}

persona_default_activation() {
  case "$1" in
    naruto|Naruto)
      printf '%s' "hey kurama take over"
      ;;
    shadow|Shadow|shadowmode|shadow_mode)
      printf '%s' "hey igris take over,hey beru take over"
      ;;
    *)
      printf '%s' "hey advisor take over"
      ;;
  esac
}

persona_default_stop() {
  case "$1" in
    naruto|Naruto)
      printf '%s' "kurama stand down"
      ;;
    shadow|Shadow|shadowmode|shadow_mode)
      printf '%s' "shadow stand down"
      ;;
    *)
      printf '%s' "advisor stand down"
      ;;
  esac
}

resolved_api_token_default() {
  local explicit
  explicit="$(env_value_or_default "TCE_API_TOKEN" "")"
  if [ -n "$explicit" ]; then
    printf '%s' "$explicit"
    return
  fi
  local token_csv
  token_csv="$(env_value_or_default "TCE_API_TOKENS" "")"
  if [ -n "$token_csv" ]; then
    local first
    first="$(first_csv_token "$token_csv")"
    if [ -n "$first" ]; then
      printf '%s' "$first"
      return
    fi
  fi
  printf '%s' "local-dev-token"
}

ensure_python3() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required but not found." >&2
    return 1
  fi
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    echo "python3 >= 3.11 is required for host runtimes (tce-capture/tce-mcp)." >&2
    return 1
  fi
  return 0
}

install_cli_capture_runtime() {
  if ! ensure_python3; then
    echo "Warning: skipping CLI capture runtime setup (python3 missing)."
    return 1
  fi

  if ! python3 -m venv "$LOCAL_CAPTURE_VENV" >/dev/null 2>&1; then
    echo "Warning: failed to create CLI capture venv."
    return 1
  fi

  "${LOCAL_CAPTURE_VENV}/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true
  if ! "${LOCAL_CAPTURE_VENV}/bin/python" -m pip install -e "${ROOT}/plugins/tce_cli_capture" >/dev/null 2>&1; then
    echo "Warning: failed to install CLI capture runtime in ${LOCAL_CAPTURE_VENV}."
    return 1
  fi

  cat > "$LOCAL_CAPTURE_LAUNCHER" <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT/.env"
  set +a
fi
: "${TCE_USER_CONSUMER_ID:=${TCE_MCP_EXECUTOR_CONSUMER_ID:-codex-executor}}"
: "${TCE_USER_ID:=${TCE_MCP_EXECUTOR_USER_ID:-${TCE_USER_CONSUMER_ID}}}"
: "${TCE_USER_WORKSPACE_ID:=${TCE_MCP_WORKSPACE_ID:-personal}}"
: "${TCE_USER_ROLE:=executor}"
export TCE_USER_CONSUMER_ID TCE_USER_ID TCE_USER_WORKSPACE_ID TCE_USER_ROLE
"${ROOT}/.venv_cli_capture/bin/tce-capture" "$@"
LAUNCHER
  chmod +x "$LOCAL_CAPTURE_LAUNCHER"
  return 0
}

install_mcp_runtime() {
  if ! ensure_python3; then
    echo "Warning: skipping MCP runtime setup (python3 missing)."
    return 1
  fi

  if ! python3 -m venv "$LOCAL_MCP_VENV" >/dev/null 2>&1; then
    echo "Warning: failed to create MCP venv."
    return 1
  fi

  "${LOCAL_MCP_VENV}/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true
  if ! "${LOCAL_MCP_VENV}/bin/python" -m pip install -e "${ROOT}/shared" -e "${ROOT}/services/tce_mcp" >/dev/null 2>&1; then
    echo "Warning: failed to install MCP runtime in ${LOCAL_MCP_VENV}."
    return 1
  fi

  cat > "$LOCAL_MCP_LAUNCHER" <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="${ROOT}/shared:${ROOT}/services/tce_mcp"
export PYTHONPATH
"${ROOT}/.venv_mcp/bin/python" -m tce_mcp.server "$@"
LAUNCHER
  chmod +x "$LOCAL_MCP_LAUNCHER"
  return 0
}

capture_cli_command_hint() {
  if [ -x "$LOCAL_CAPTURE_LAUNCHER" ]; then
    printf '%s' "$LOCAL_CAPTURE_LAUNCHER"
    return
  fi
  if command -v tce-capture >/dev/null 2>&1; then
    printf '%s' "tce-capture"
    return
  fi
  printf '%s' "python3 -m tce_cli_capture.cli"
}

mcp_server_command_hint() {
  if [ -x "$LOCAL_MCP_LAUNCHER" ]; then
    printf '%s' "$LOCAL_MCP_LAUNCHER"
  else
    printf '%s' "python3 -m tce_mcp.server"
  fi
}

uninstall_host_runtimes() {
  rm -f "$LOCAL_CAPTURE_LAUNCHER" >/dev/null 2>&1 || true
  rm -rf "$LOCAL_CAPTURE_VENV" >/dev/null 2>&1 || true
  rm -f "$LOCAL_MCP_LAUNCHER" >/dev/null 2>&1 || true
  rm -rf "$LOCAL_MCP_VENV" >/dev/null 2>&1 || true

  if command -v python3 >/dev/null 2>&1; then
    python3 -m pip uninstall -y tce-cli-capture >/dev/null 2>&1 || true
    python3 -m pip uninstall -y tce_cli_capture >/dev/null 2>&1 || true
    python3 -m pip uninstall -y tce-mcp >/dev/null 2>&1 || true
    python3 -m pip uninstall -y tce_mcp >/dev/null 2>&1 || true
    rm -f "$HOME/.local/bin/tce-capture" >/dev/null 2>&1 || true
  fi
}

ensure_maintenance_cron() {
  if [ ! -x "${ROOT}/scripts/enqueue_maintenance.sh" ]; then
    echo "Warning: scripts/enqueue_maintenance.sh missing or not executable."
    return 1
  fi
  if ! command -v crontab >/dev/null 2>&1; then
    echo "Warning: crontab not found. Install cron or schedule ${ROOT}/scripts/enqueue_maintenance.sh manually."
    return 1
  fi

  local schedule
  schedule="${TCE_MAINTENANCE_CRON:-$MAINTENANCE_CRON_DEFAULT_EXPR}"
  local cron_line
  cron_line="${schedule} cd \"${ROOT}\" && bash \"${ROOT}/scripts/enqueue_maintenance.sh\" >> \"${ROOT}/install.log\" 2>&1 ${MAINTENANCE_CRON_MARKER}"

  local existing filtered
  existing="$(crontab -l 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$existing" | grep -Fv "$MAINTENANCE_CRON_MARKER" || true)"
  if [ -n "$filtered" ]; then
    printf '%s\n%s\n' "$filtered" "$cron_line" | crontab -
  else
    printf '%s\n' "$cron_line" | crontab -
  fi
  echo "Lifecycle scheduler: weekly cron installed (${schedule})."
  return 0
}

remove_maintenance_cron() {
  if ! command -v crontab >/dev/null 2>&1; then
    return 0
  fi
  local existing filtered
  existing="$(crontab -l 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$existing" | grep -Fv "$MAINTENANCE_CRON_MARKER" || true)"
  if [ -z "$filtered" ]; then
    crontab -r >/dev/null 2>&1 || true
  else
    printf '%s\n' "$filtered" | crontab -
  fi
  echo "Lifecycle scheduler cron removed."
  return 0
}

ensure_backup_cron() {
  # Opt-in nightly backup via scripts/db_backup.sh (the path that actually
  # works against the named-volume stack), with retention pruning.
  if [ ! -x "${ROOT}/scripts/db_backup.sh" ]; then
    echo "Warning: scripts/db_backup.sh missing or not executable."
    return 1
  fi
  if ! command -v crontab >/dev/null 2>&1; then
    echo "Warning: crontab not found. Schedule ${ROOT}/scripts/db_backup.sh manually."
    return 1
  fi

  local schedule retention prune_cmd cron_line existing filtered
  schedule="${TCE_BACKUP_CRON:-$BACKUP_CRON_DEFAULT_EXPR}"
  retention="${TCE_BACKUP_RETENTION_DAYS:-$BACKUP_RETENTION_DAYS_DEFAULT}"
  prune_cmd="find \"${ROOT}/backups/manual\" -type f -name 'tce_*' -mtime +${retention} -delete"
  cron_line="${schedule} cd \"${ROOT}\" && bash \"${ROOT}/scripts/db_backup.sh\" --reason nightly && ${prune_cmd} >> \"${ROOT}/install.log\" 2>&1 ${BACKUP_CRON_MARKER}"

  existing="$(crontab -l 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$existing" | grep -Fv "$BACKUP_CRON_MARKER" || true)"
  if [ -n "$filtered" ]; then
    printf '%s\n%s\n' "$filtered" "$cron_line" | crontab -
  else
    printf '%s\n' "$cron_line" | crontab -
  fi
  echo "Backup scheduler: nightly cron installed (${schedule}, retention ${retention}d)."
  return 0
}

remove_backup_cron() {
  if ! command -v crontab >/dev/null 2>&1; then
    return 0
  fi
  local existing filtered
  existing="$(crontab -l 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$existing" | grep -Fv "$BACKUP_CRON_MARKER" || true)"
  if [ -z "$filtered" ]; then
    crontab -r >/dev/null 2>&1 || true
  else
    printf '%s\n' "$filtered" | crontab -
  fi
  echo "Backup scheduler cron removed."
  return 0
}

configure_maintenance_scheduler_for_stack() {
  local mode="$1"
  case "$mode" in
    lite)
      echo "Lifecycle scheduler: skipped for lite stack."
      ;;
    *)
      ensure_maintenance_cron || true
      # Nightly backups are opt-in: set TCE_ENABLE_NIGHTLY_BACKUP=1 to enable.
      if [ "${TCE_ENABLE_NIGHTLY_BACKUP:-0}" = "1" ]; then
        ensure_backup_cron || true
      else
        echo "Backup scheduler: disabled (set TCE_ENABLE_NIGHTLY_BACKUP=1 to enable nightly db_backup.sh)."
      fi
      ;;
  esac
}

ensure_autonomy_tick_cron() {
  if [ ! -x "${ROOT}/scripts/enqueue_autonomy_tick.sh" ]; then
    echo "Warning: scripts/enqueue_autonomy_tick.sh missing or not executable."
    return 1
  fi
  if ! command -v crontab >/dev/null 2>&1; then
    echo "Warning: crontab not found. Install cron or schedule ${ROOT}/scripts/enqueue_autonomy_tick.sh manually."
    return 1
  fi

  local schedule
  schedule="${TCE_AUTONOMY_TICK_CRON:-$AUTONOMY_TICK_CRON_DEFAULT_EXPR}"
  local cron_line
  cron_line="${schedule} cd \"${ROOT}\" && bash \"${ROOT}/scripts/enqueue_autonomy_tick.sh\" >> \"${ROOT}/install.log\" 2>&1 ${AUTONOMY_TICK_CRON_MARKER}"

  local existing filtered
  existing="$(crontab -l 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$existing" | grep -Fv "$AUTONOMY_TICK_CRON_MARKER" || true)"
  if [ -n "$filtered" ]; then
    printf '%s\n%s\n' "$filtered" "$cron_line" | crontab -
  else
    printf '%s\n' "$cron_line" | crontab -
  fi
  echo "Autonomy tick scheduler: configured (${schedule})."
  return 0
}

remove_autonomy_tick_cron() {
  if ! command -v crontab >/dev/null 2>&1; then
    return 0
  fi
  local existing filtered
  existing="$(crontab -l 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$existing" | grep -Fv "$AUTONOMY_TICK_CRON_MARKER" || true)"
  if [ -z "$filtered" ]; then
    crontab -r >/dev/null 2>&1 || true
  else
    printf '%s\n' "$filtered" | crontab -
  fi
  echo "Autonomy tick scheduler cron removed."
  return 0
}

configure_autonomy_tick_scheduler_for_mode() {
  local operation_mode="$1"
  if [ "$operation_mode" = "clone_advisor" ]; then
    ensure_autonomy_tick_cron || true
  else
    remove_autonomy_tick_cron || true
    echo "Autonomy tick scheduler: skipped (mode=${operation_mode})."
  fi
}

bootstrap_timeline_data() {
  local api_url="$1"
  local api_token="$2"
  local workspace_id="$3"
  local user_id="$4"
  local db_url="$5"

  if [ ! -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
    return
  fi
  if ! ensure_python3; then
    echo "python3 not found; skipping bootstrap."
    return
  fi

  local import_choice="Y"
  if [ "$ASSUME_YES" != "true" ] && [ -t 0 ]; then
    echo "Bootstrap timeline data (optional)"
    read -r -p "Import existing git history now? [Y/n]: " import_choice
  fi
  case "${import_choice:-Y}" in
    n|N|no|NO)
      ;;
    *)
      local default_repo="$ROOT"
      local repo_path="$default_repo"
      if [ "$ASSUME_YES" != "true" ] && [ -t 0 ]; then
        read -r -p "Git repo path [${default_repo}]: " repo_input
        repo_path="${repo_input:-$default_repo}"
      fi
      if ! python3 "${ROOT}/scripts/import_git_history.py" \
        --repo "$repo_path" \
        --api-url "$api_url" \
        --token "$api_token" \
        --workspace "$workspace_id" \
        --user "$user_id" \
        --consumer "install-bootstrap" \
        --max-commits 150; then
        echo "Warning: git history import failed."
      fi
      ;;
  esac

  if [ "$STACK_MODE" = "full" ]; then
    local seed_choice="Y"
    if [ "$ASSUME_YES" != "true" ] && [ -t 0 ]; then
      read -r -p "Seed starter patterns now? [Y/n]: " seed_choice
    fi
    case "${seed_choice:-Y}" in
      n|N|no|NO)
        ;;
      *)
        if ! (cd "$ROOT" && TCE_DATABASE_URL="$db_url" PYTHONPATH="services/tce_api:shared" python3 scripts/seed_starter_patterns.py); then
          echo "Warning: starter pattern seeding failed."
        fi
        ;;
    esac
  else
    echo "Starter pattern DB seed skipped for lite stack."
  fi
}

configure_mcp_clients_prompt() {
  local api_url="$1"
  local api_token="$2"
  local workspace_id="$3"
  local user_id="$4"
  local client_targets="${5:-all}"
  local workspace_mode="${6:-shared}"
  local workspace_map="${7:-}"
  local identity_map="${8:-}"
  local consumer_map="${9:-}"
  local session_map="${10:-}"

  if [ ! -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
    return
  fi

  local mcp_choice="Y"
  if [ "$ASSUME_YES" != "true" ] && [ -t 0 ]; then
    echo "Configure MCP clients (optional)"
    read -r -p "Write MCP config for selected executors (${client_targets}) now? [Y/n]: " mcp_choice
  fi
  case "${mcp_choice:-Y}" in
    n|N|no|NO)
      return
      ;;
  esac

  if [ ! -x "${ROOT}/scripts/configure_mcp_clients.sh" ]; then
    echo "Warning: scripts/configure_mcp_clients.sh not executable or missing."
    return
  fi
  if ! "${ROOT}/scripts/configure_mcp_clients.sh" \
    --client "$client_targets" \
    --api-url "$api_url" \
    --token "$api_token" \
    --workspace "$workspace_id" \
    --workspace-mode "$workspace_mode" \
    --workspace-map "$workspace_map" \
    --identity-map "$identity_map" \
    --consumer-map "$consumer_map" \
    --session-map "$session_map" \
    --user-id "$user_id"; then
    echo "Warning: MCP client config script failed."
  fi
}

apply_takeover_policy_prompt() {
  if [ ! -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
    return
  fi
  if [ ! -x "${ROOT}/scripts/apply_takeover_policy.sh" ]; then
    echo "Warning: scripts/apply_takeover_policy.sh not executable or missing."
    return
  fi

  local workspace_default="${PWD}"
  local workspace_root="$workspace_default"
  local policy_args=("--workspace-root" "$workspace_root")

  if [ "$ASSUME_YES" = "true" ]; then
    # Auto-apply with defaults in unattended mode
    if ! "${ROOT}/scripts/apply_takeover_policy.sh" "${policy_args[@]}"; then
      echo "Warning: workspace takeover policy setup failed."
    fi
    return
  fi

  echo "Apply takeover auto-call policy (recommended)"
  read -r -p "Apply workspace policy now [Y/n]: " apply_policy_choice
  case "${apply_policy_choice:-Y}" in
    n|N|no|NO)
      return
      ;;
  esac

  read -r -p "Workspace root for policy files [${workspace_default}]: " workspace_root_input
  workspace_root="${workspace_root_input:-$workspace_default}"
  policy_args=("--workspace-root" "$workspace_root")

  read -r -p "Copy policy text to clipboard [Y/n]: " copy_choice
  case "${copy_choice:-Y}" in
    n|N|no|NO)
      ;;
    *)
      policy_args+=("--copy-policy")
      ;;
  esac

  read -r -p "Open Claude settings helper now [y/N]: " open_choice
  case "${open_choice:-N}" in
    y|Y|yes|YES)
      policy_args+=("--open-claude")
      ;;
  esac

  if ! "${ROOT}/scripts/apply_takeover_policy.sh" "${policy_args[@]}"; then
    echo "Warning: workspace takeover policy setup failed."
  fi
}

workspace_takeover_policy_active() {
  local workspace_root="$1"
  local workspace_agents="${workspace_root}/AGENTS.md"
  local workspace_claude="${workspace_root}/CLAUDE.md"

  if [ -f "$workspace_agents" ] && grep -Fq "tce.takeover_step" "$workspace_agents"; then
    return 0
  fi
  if [ -f "$workspace_claude" ] && grep -Fq "tce.takeover_step" "$workspace_claude"; then
    return 0
  fi
  return 1
}

ensure_workspace_takeover_policy() {
  local workspace_root="$1"
  local operation_mode="$2"

  if [ "$operation_mode" != "clone_advisor" ]; then
    return 0
  fi

  if workspace_takeover_policy_active "$workspace_root"; then
    echo "Workspace takeover policy: active (${workspace_root})"
    return 0
  fi

  if [ -x "${ROOT}/scripts/apply_takeover_policy.sh" ]; then
    if "${ROOT}/scripts/apply_takeover_policy.sh" --workspace-root "$workspace_root" >/dev/null 2>&1; then
      echo "Workspace takeover policy: applied (${workspace_root})"
      return 0
    fi
  fi

  echo "Warning: workspace takeover policy missing in ${workspace_root}."
  echo "Run: ${ROOT}/scripts/apply_takeover_policy.sh --workspace-root \"${workspace_root}\""
  return 1
}

install_client_hooks() {
  local workspace_root="$1"
  local self_heal_path="${ROOT}/scripts/self-heal.sh"

  if ! command -v python3 >/dev/null 2>&1; then
    echo "  python3 not found; skipping client hook setup."
    return 1
  fi

  echo "Step 12 - Client hooks"

  local hooks_json
  hooks_json=$(python3 -c "
import json, sys
self_heal = sys.argv[1]
hooks = {
    'hooks': {
        'UserPromptSubmit': [{
                'matcher': '*',
                'hooks': [{
                    'type': 'command',
                'command': 'echo \'{\"systemMessage\": \"[TCE-HOOK] Conditional takeover policy: scan chat history for activation and stand-down state. Call mcp__tce-executor__tce_takeover_step only on activation turns and while takeover is active (use executor session_id, for example codex/claude, with activation_mode_default=takeover). On stand-down turns, call mcp__tce-executor__tce_reset_takeover_state. While inactive, respond naturally and do not call takeover_step or check_context in normal flow. If has_directive=true, execute the objective immediately. If safety_decision=confirm_required, ask for confirmation.\"}\'',
                'timeout': 10
            }]
        }],
        'PostToolUse': [{
            'matcher': 'Edit|Write',
            'hooks': [{
                'type': 'command',
                'command': 'if echo \"\$TOOL_INPUT\" | grep -q \"open-timeline-engine\"; then echo \\'{{\"systemMessage\": \"[TCE-SELF-HEAL] You modified a TCE file. Run: bash ' + self_heal + ' then continue the current workflow. Call mcp__tce-executor__tce_takeover_step only if takeover is active in this chat; otherwise continue naturally.\"}}\\'; else echo \\'{{\"continue\": true}}\\'; fi',
                'timeout': 10
            }]
        }],
        'Stop': [{
            'matcher': '*',
            'hooks': [{
                'type': 'command',
                'command': 'echo \'{"systemMessage": "[TCE-COMPLETION] Before ending after any mutating change, call mcp__tce-executor__tce_complete_task with the title, touched files, decision, outcome/next step, git refs, and anchors. Skip only when tce.report_execution already captured this completion."}\'',
                'timeout': 10
            }]
        }]
    }
}
print(json.dumps(hooks, indent=2))
" "$self_heal_path" 2>/dev/null) || {
    echo "  Failed to generate hooks JSON."
    return 1
  }

  local installed_any="false"

  # Claude Code
  local claude_settings_dir="${workspace_root}/.claude"
  local claude_settings="${claude_settings_dir}/settings.json"
  if [ -d "$claude_settings_dir" ] || [ -d "${HOME}/.claude" ]; then
    mkdir -p "$claude_settings_dir"
    if [ -f "$claude_settings" ]; then
      python3 -c "
import json, sys
existing = json.load(open(sys.argv[1]))
new_hooks = json.loads(sys.argv[2])
existing.setdefault('hooks', {})
existing['hooks'].update(new_hooks.get('hooks', {}))
json.dump(existing, open(sys.argv[1], 'w'), indent=2)
print('  Merged hooks into ' + sys.argv[1])
" "$claude_settings" "$hooks_json"
    else
      echo "$hooks_json" > "$claude_settings"
      echo "  Created ${claude_settings}"
    fi
    installed_any="true"
  fi

  # Cursor
  local cursor_settings_dir="${workspace_root}/.cursor"
  if [ -d "$cursor_settings_dir" ] || command -v cursor >/dev/null 2>&1; then
    mkdir -p "$cursor_settings_dir"
    local cursor_settings="${cursor_settings_dir}/settings.json"
    if [ -f "$cursor_settings" ]; then
      python3 -c "
import json, sys
existing = json.load(open(sys.argv[1]))
new_hooks = json.loads(sys.argv[2])
existing.setdefault('hooks', {})
existing['hooks'].update(new_hooks.get('hooks', {}))
json.dump(existing, open(sys.argv[1], 'w'), indent=2)
print('  Merged hooks into ' + sys.argv[1])
" "$cursor_settings" "$hooks_json"
    else
      echo "$hooks_json" > "$cursor_settings"
      echo "  Created ${cursor_settings}"
    fi
    installed_any="true"
  fi

  # Codex
  local codex_home="${CODEX_HOME:-${HOME}/.codex}"
  if [ -d "$codex_home" ] || command -v codex >/dev/null 2>&1; then
    echo "  [NOTE] Codex detected. Hook JSON saved for manual setup."
    mkdir -p "${ROOT}/docs/mcp-config/generated"
    echo "$hooks_json" > "${ROOT}/docs/mcp-config/generated/client_hooks.json"
    installed_any="true"
  fi

  if [ "$installed_any" = "false" ]; then
    echo "  No supported clients detected. Saving hooks to docs/mcp-config/generated/client_hooks.json"
    mkdir -p "${ROOT}/docs/mcp-config/generated"
    echo "$hooks_json" > "${ROOT}/docs/mcp-config/generated/client_hooks.json"
  fi
}

remove_policy_block_from_file() {
  local file_path="$1"
  local start_marker="$2"
  local end_marker="$3"

  if [ ! -f "$file_path" ]; then
    return 1
  fi
  if ! grep -Fq "$start_marker" "$file_path"; then
    return 1
  fi

  local tmp
  tmp="$(mktemp)"
  awk -v start="$start_marker" -v end="$end_marker" '
    BEGIN { skipping=0 }
    index($0, start) { skipping=1; next }
    index($0, end) { skipping=0; next }
    skipping==0 { print }
  ' "$file_path" > "$tmp"
  mv "$tmp" "$file_path"
  return 0
}

remove_workspace_takeover_policy() {
  local workspace_root="$1"
  local removed_any="false"
  local file_path

  for file_path in "${workspace_root}/AGENTS.md" "${workspace_root}/CLAUDE.md"; do
    if remove_policy_block_from_file "$file_path" "$TAKEOVER_POLICY_START_MARKER" "$TAKEOVER_POLICY_END_MARKER"; then
      echo "Removed takeover policy block: ${file_path}"
      removed_any="true"
    fi
  done

  if [ "$removed_any" = "false" ]; then
    echo "No takeover policy blocks found in ${workspace_root}/AGENTS.md or ${workspace_root}/CLAUDE.md"
  fi
}

install_failed() {
  echo
  echo "Install failed. Check log: ${INSTALL_LOG:-install.log}"
  restore_env
  exit 1
}
trap install_failed ERR

preflight_checks() {
  echo "Pre-flight checks"
  local errors=0

  # Hard checks
  if ! command -v docker >/dev/null 2>&1; then
    echo "  [FAIL] docker not found. Install Docker Desktop/Engine first."
    errors=$((errors + 1))
  elif ! docker info >/dev/null 2>&1; then
    echo "  [FAIL] Docker daemon not running. Start Docker Desktop first."
    errors=$((errors + 1))
  else
    echo "  [PASS] Docker available and running"
  fi

  if ! docker compose version >/dev/null 2>&1; then
    echo "  [FAIL] docker compose not available."
    errors=$((errors + 1))
  else
    echo "  [PASS] Docker Compose available"
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "  [FAIL] curl not found (needed for health checks)."
    errors=$((errors + 1))
  else
    echo "  [PASS] curl available"
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "  [WARN] python3 not found (host runtimes will be skipped)"
  elif ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    echo "  [WARN] python3 < 3.11 (host runtimes require 3.11+)"
  else
    echo "  [PASS] Python 3.11+ available"
  fi

  # Soft checks
  local port_conflict="false"
  for port in 8080 5432 6379; do
    if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$port" -sTCP:LISTEN -P -n >/dev/null 2>&1; then
      echo "  [WARN] Port $port already in use"
      port_conflict="true"
    fi
  done
  if [ "$port_conflict" = "false" ]; then
    echo "  [PASS] Required ports available (8080, 5432, 6379)"
  fi

  if command -v df >/dev/null 2>&1; then
    local free_kb
    free_kb="$(df -k "$ROOT" | awk 'NR==2 {print $4}')"
    if [ -n "$free_kb" ] && [ "$free_kb" -lt 2097152 ]; then
      echo "  [WARN] Less than 2GB free disk space"
    else
      echo "  [PASS] Sufficient disk space"
    fi
  fi

  if [ "$errors" -gt 0 ]; then
    echo
    echo "Pre-flight failed with ${errors} error(s). Fix the issues above and re-run."
    exit 1
  fi

  if [ "$port_conflict" = "true" ] && [ "$ASSUME_YES" != "true" ] && [ -t 0 ]; then
    read -r -p "Port conflicts detected. Continue anyway? [y/N]: " continue_choice
    case "${continue_choice:-N}" in
      y|Y|yes|YES) ;;
      *) echo "Aborting."; exit 0 ;;
    esac
  fi

  echo
}

if [ "$ACTION" = "install" ] || [ "$ACTION" = "fix" ] || [ "$ACTION" = "restart" ]; then
  preflight_checks
fi

echo "Open Timeline Engine setup"
echo

if [ "$ACTION" = "install" ] && [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
  echo "Step 0 - Choose action"
  echo "1) Install/start stack (default)"
  echo "2) Restart stack (rebuild images)"
  echo "3) Stop stack"
  echo "4) Remove stack + local data"
  echo "5) Run doctor checks (report only)"
  echo "6) Fix common setup issues"
  echo "7) Create manual stack backup snapshot (Postgres + Qdrant)"
  echo "8) Restore stack from backup snapshot"
  read -r -p "Select action [1/2/3/4/5/6/7/8]: " action_choice
  case "$action_choice" in
    2|restart|Restart|RESTART|rebuild|Rebuild|REBUILD)
      ACTION="restart"
      ;;
    3|stop|Stop|STOP)
      ACTION="stop"
      ;;
    4|remove|Remove|REMOVE|uninstall|Uninstall)
      ACTION="remove"
      ;;
    5|doctor|Doctor|DOCTOR)
      ACTION="doctor"
      ;;
    6|fix|Fix|FIX|repair|Repair|REPAIR)
      ACTION="fix"
      ;;
    7|backup|Backup|BACKUP)
      ACTION="backup"
      ;;
    8|restore|Restore|RESTORE)
      ACTION="restore"
      ;;
    *)
      ACTION="install"
      ;;
  esac
fi

if [ "$ACTION" != "install" ] && [ "$ACTION" != "backup" ] && [ "$ACTION" != "restore" ] && [ "$STACK_SET_BY_ARG" != "true" ]; then
  if [ -t 0 ]; then
    echo "Choose stack target"
    echo "1) Full stack (recommended)"
    echo "2) Lightweight stack"
    echo "3) Both stacks (default)"
    read -r -p "Select target [1/2/3]: " stop_choice
    case "$stop_choice" in
      1|full|Full|FULL)
        STACK_MODE="full"
        ;;
      2|lite|Lite|LITE)
        STACK_MODE="lite"
        ;;
      *)
        STACK_MODE="all"
        ;;
    esac
  else
    STACK_MODE="all"
  fi
fi

if [ "$ACTION" = "doctor" ]; then
  "${ROOT}/scripts/doctor.sh" "$STACK_MODE"
  exit $?
fi

if [ "$ACTION" = "backup" ]; then
  if [ ! -x "${ROOT}/scripts/db_backup.sh" ]; then
    echo "Backup helper missing: ${ROOT}/scripts/db_backup.sh" >&2
    exit 1
  fi
  "${ROOT}/scripts/db_backup.sh"
  exit $?
fi

if [ "$ACTION" = "restore" ]; then
  if [ ! -x "${ROOT}/scripts/db_restore.sh" ]; then
    echo "Restore helper missing: ${ROOT}/scripts/db_restore.sh" >&2
    exit 1
  fi
  if [ -n "$RESTORE_BACKUP_FILE" ]; then
    "${ROOT}/scripts/db_restore.sh" --file "$RESTORE_BACKUP_FILE"
  else
    "${ROOT}/scripts/db_restore.sh" --latest
  fi
  restore_rc=$?
  if [ "$restore_rc" -eq 0 ]; then
    echo "Running doctor checks after restore..."
    "${ROOT}/scripts/doctor.sh" full || true
  fi
  exit "$restore_rc"
fi

if [ "$ACTION" = "stop" ]; then
  "${ROOT}/scripts/stop.sh" "$STACK_MODE"
  echo "Stopped stack target: ${STACK_MODE}"
  exit 0
fi

if [ "$ACTION" = "restart" ]; then
  if [ ! -x "${ROOT}/scripts/db_backup.sh" ]; then
    echo "Backup helper missing: ${ROOT}/scripts/db_backup.sh" >&2
    exit 1
  fi
  echo "Creating pre-restart DB snapshot..."
  "${ROOT}/scripts/db_backup.sh" --reason "pre_restart_${STACK_MODE}"

  if [ "$STACK_MODE" = "all" ] || [ "$STACK_MODE" = "auto" ]; then
    echo "Restarting full stack (rebuild)..."
    "${ROOT}/scripts/stop.sh" full
    "${ROOT}/scripts/start.sh" full --detach
    echo "Restarting lightweight stack (rebuild)..."
    "${ROOT}/scripts/stop.sh" lite
    "${ROOT}/scripts/start.sh" lite --detach
    ensure_qdrant_ready_for_stack "full" || true
    echo "Restart complete: all stacks"
  else
    echo "Restarting stack (${STACK_MODE}) with rebuild..."
    "${ROOT}/scripts/stop.sh" "$STACK_MODE"
    "${ROOT}/scripts/start.sh" "$STACK_MODE" --detach
    ensure_qdrant_ready_for_stack "$STACK_MODE" || true
    echo "Restart complete: ${STACK_MODE}"
  fi
  restart_operation_mode="$(env_value_or_default "TCE_DEFAULT_OPERATION_MODE" "timeline_only")"
  ensure_workspace_takeover_policy "${PWD}" "$restart_operation_mode" || true
  configure_maintenance_scheduler_for_stack "$STACK_MODE"
  configure_autonomy_tick_scheduler_for_mode "$restart_operation_mode"
  exit 0
fi

if [ "$ACTION" = "fix" ]; then
  if [ ! -f "$ENV_FILE" ]; then
    cp "${ROOT}/.env.example" "$ENV_FILE"
    echo "Created ${ENV_FILE} from .env.example"
  fi

  requested_stack_mode="$STACK_MODE"
  resolved_stack_mode="$STACK_MODE"
  if [ "$STACK_MODE" = "all" ] || [ "$STACK_MODE" = "auto" ]; then
    full_services="$(docker compose -f "${ROOT}/infra/docker-compose.yml" ps --status running --services 2>/dev/null || true)"
    lite_services="$(docker compose -f "${ROOT}/infra/docker-compose.lite.yml" ps --status running --services 2>/dev/null || true)"
    if [ -n "$lite_services" ] && [ -z "$full_services" ]; then
      resolved_stack_mode="lite"
    else
      resolved_stack_mode="full"
    fi
    echo "Resolved fix target: ${resolved_stack_mode} (requested ${requested_stack_mode})"
  fi

  echo "Running automated fix for ${resolved_stack_mode} ..."
  "${ROOT}/scripts/start.sh" "$resolved_stack_mode" --detach
  ensure_qdrant_ready_for_stack "$resolved_stack_mode" || true

  api_port="$(env_value_or_default "TCE_API_PORT" "8080")"
  api_url="http://localhost:${api_port}"
  api_token="$(resolved_api_token_default)"
  workspace_id="$(env_value_or_default "TCE_MCP_WORKSPACE_ID" "personal")"
  workspace_mode="$(env_value_or_default "TCE_MCP_WORKSPACE_MODE" "shared")"
  workspace_map="$(env_value_or_default "TCE_MCP_WORKSPACE_MAP" "")"
  executor_clients_fix="$(env_value_or_default "TCE_EXECUTOR_CLIENTS" "$EXECUTOR_CLIENTS")"
  normalize_executor_clients "$executor_clients_fix"
  executor_clients_fix="$EXECUTOR_CLIENTS"
  primary_client_fix="$(first_csv_token "$executor_clients_fix")"
  secondary_client_fix="$(second_csv_token "$executor_clients_fix")"
  [ -z "$primary_client_fix" ] && primary_client_fix="codex"
  [ -z "$secondary_client_fix" ] && secondary_client_fix="claude"
  primary_user_fix="$(env_value_or_default "TCE_MCP_EXECUTOR_USER_ID" "${primary_client_fix}-executor")"
  primary_consumer_fix="$(env_value_or_default "TCE_MCP_EXECUTOR_CONSUMER_ID" "$primary_user_fix")"
  primary_session_fix="$(env_value_or_default "TCE_MCP_EXECUTOR_SESSION_ID" "$primary_client_fix")"
  secondary_user_fix="$(env_value_or_default "TCE_MCP_SECONDARY_USER_ID" "${secondary_client_fix}-executor")"
  secondary_consumer_fix="$(env_value_or_default "TCE_MCP_SECONDARY_CONSUMER_ID" "$secondary_user_fix")"
  secondary_session_fix="$(env_value_or_default "TCE_MCP_SECONDARY_SESSION_ID" "$secondary_client_fix")"
  identity_map_fix="$(build_identity_map_for_clients "$executor_clients_fix" "$primary_user_fix" "$secondary_user_fix")"
  consumer_map_fix="$(build_identity_map_for_clients "$executor_clients_fix" "$primary_consumer_fix" "$secondary_consumer_fix")"
  session_map_fix="$(build_session_map_for_clients "$executor_clients_fix" "$primary_session_fix" "$secondary_session_fix")"
  default_user_id="${USER:-local-user}"
  user_id="$(env_value_or_default "TCE_MCP_EXECUTOR_USER_ID" "$(env_value_or_default "TCE_MCP_USER_ID" "$default_user_id")")"

  if command -v curl >/dev/null 2>&1; then
    for _ in $(seq 1 60); do
      if curl -fsS "${api_url}/v1/health" >/dev/null 2>&1; then
        break
      fi
      sleep 2
    done
  fi

  install_cli_capture_runtime || true
  install_mcp_runtime || true

  if [ -x "${ROOT}/scripts/self-heal.sh" ]; then
    echo "Running self-heal (rebuild + re-activate takeover)..."
    "${ROOT}/scripts/self-heal.sh" --service tce-api || true
  fi

  if [ -x "${ROOT}/scripts/configure_mcp_clients.sh" ]; then
    "${ROOT}/scripts/configure_mcp_clients.sh" \
      --client "$executor_clients_fix" \
      --api-url "$api_url" \
      --token "$api_token" \
      --workspace "$workspace_id" \
      --workspace-mode "$workspace_mode" \
      --workspace-map "$workspace_map" \
      --identity-map "$identity_map_fix" \
      --consumer-map "$consumer_map_fix" \
      --session-map "$session_map_fix" \
      --user-id "$user_id" || true
  else
    echo "Warning: scripts/configure_mcp_clients.sh not executable or missing."
  fi

  install_client_hooks "${PWD}" || true

  operation_mode_fix="$(env_value_or_default "TCE_DEFAULT_OPERATION_MODE" "timeline_only")"
  ensure_workspace_takeover_policy "${PWD}" "$operation_mode_fix" || true
  configure_maintenance_scheduler_for_stack "$resolved_stack_mode"
  configure_autonomy_tick_scheduler_for_mode "$operation_mode_fix"

  if [ "$resolved_stack_mode" = "full" ] && [ -x "${ROOT}/scripts/db_restore.sh" ] && command -v docker >/dev/null 2>&1; then
    latest_backup_path="$("${ROOT}/scripts/db_restore.sh" --print-latest 2>/dev/null || true)"
    if [ -n "$latest_backup_path" ]; then
      event_count="$(docker compose -f "${ROOT}/infra/docker-compose.yml" exec -T postgres psql -U postgres -d tce -Atc \"SELECT COUNT(*) FROM events\" 2>/dev/null || echo \"\")"
      if [ "${event_count:-0}" = "0" ]; then
        echo
        echo "Timeline is currently empty. Restore helper available:"
        echo "  ./scripts/install.sh restore --backup-file \"${latest_backup_path}\""
      fi
    fi
  fi

  echo
  echo "Post-fix diagnostics:"
  "${ROOT}/scripts/doctor.sh" "$resolved_stack_mode"
  exit $?
fi

if [ "$ACTION" = "remove" ]; then
  echo
  echo "This will remove containers, networks, volumes, local env file, and local host runtimes."
  wipe_phrase="${TCE_WIPE_CONFIRM_PHRASE:-WIPE TCE DATA}"
  wipe_confirm_token="${TCE_WIPE_CONFIRM_TOKEN:-}"
  if [ "$ASSUME_YES" != "true" ]; then
    if [ -t 0 ]; then
      read -r -p "Type REMOVE to confirm: " confirm_remove
      if [ "$confirm_remove" != "REMOVE" ]; then
        echo "Remove cancelled."
        exit 0
      fi
      read -r -p "Type '${wipe_phrase}' to confirm permanent data wipe: " wipe_confirm_token
      if [ "$wipe_confirm_token" != "$wipe_phrase" ]; then
        echo "Data wipe confirmation mismatch. Remove cancelled."
        exit 0
      fi
    else
      echo "Non-interactive remove requires --yes."
      exit 1
    fi
  else
    if [ -z "$wipe_confirm_token" ]; then
      if [ -t 0 ]; then
        read -r -p "Type '${wipe_phrase}' to confirm permanent data wipe: " wipe_confirm_token
      fi
    fi
    if [ "$wipe_confirm_token" != "$wipe_phrase" ]; then
      echo "Remove requires wipe confirmation phrase '${wipe_phrase}' via prompt or TCE_WIPE_CONFIRM_TOKEN." >&2
      exit 1
    fi
  fi
  if [ "$REMOVE_WORKSPACE_POLICY" != "true" ] && [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
    read -r -p "Also remove takeover policy blocks from workspace files in ${PWD}? [y/N]: " remove_policy_choice
    case "${remove_policy_choice:-N}" in
      y|Y|yes|YES)
        REMOVE_WORKSPACE_POLICY="true"
        ;;
    esac
  fi
  "${ROOT}/scripts/stop.sh" "$STACK_MODE" --remove-data --confirm-wipe "$wipe_confirm_token" --remove-images --purge-local-state
  uninstall_host_runtimes
  remove_maintenance_cron || true
  remove_autonomy_tick_cron || true
  remove_backup_cron || true
  if [ "$REMOVE_WORKSPACE_POLICY" = "true" ]; then
    remove_workspace_takeover_policy "${PWD}"
  else
    echo "Workspace takeover policy blocks kept. Use --remove-policy or re-run remove to delete them."
  fi
  echo "Removed stack target: ${STACK_MODE}"
  exit 0
fi

if [ "$STACK_MODE" = "all" ] || [ "$STACK_MODE" = "auto" ]; then
  STACK_MODE="full"
fi

if [ ! -f "$ENV_FILE" ]; then
  cp "${ROOT}/.env.example" "$ENV_FILE"
  ENV_CREATED_THIS_RUN="true"
fi

if [ "$STACK_SET_BY_ARG" != "true" ] && [ -t 0 ]; then
  echo "Step 1 - Choose stack"
  echo "1) Full production stack (recommended, default)"
  echo "2) Lightweight stack"
  read -r -p "Select stack [1/2]: " stack_choice
  case "$stack_choice" in
    2|lite|Lite|LITE)
      STACK_MODE="lite"
      ;;
    *)
      STACK_MODE="full"
      ;;
  esac
else
  if [ "$STACK_MODE" != "lite" ]; then
    STACK_MODE="full"
  fi
  echo "Step 1 - Choose stack"
fi
echo "Selected stack: ${STACK_MODE}"
echo

existing_runtime_profile="$(env_value_or_default "TCE_RUNTIME_PROFILE" "")"
if [ "$RUNTIME_PROFILE_SET_BY_ARG" != "true" ]; then
  case "$existing_runtime_profile" in
    local-lite|local-full|team-secure|research)
      RUNTIME_PROFILE="$existing_runtime_profile"
      ;;
    *)
      if [ "$STACK_MODE" = "lite" ]; then
        RUNTIME_PROFILE="local-lite"
      else
        RUNTIME_PROFILE="local-full"
      fi
      ;;
  esac
fi

if [ "$RUNTIME_PROFILE_SET_BY_ARG" = "true" ] || [ "$ENV_CREATED_THIS_RUN" = "true" ] || [ -z "$existing_runtime_profile" ]; then
  backup_env
  profile_args=(--profile "$RUNTIME_PROFILE" --env-file "$ENV_FILE")
  if [ "$RUNTIME_PROFILE_SET_BY_ARG" = "true" ] || [ "$ENV_CREATED_THIS_RUN" = "true" ]; then
    profile_args+=(--overwrite)
  fi
  "${ROOT}/scripts/apply_runtime_profile.sh" "${profile_args[@]}"
else
  echo "Using runtime profile from existing .env: ${RUNTIME_PROFILE}"
fi
echo "Selected runtime profile: ${RUNTIME_PROFILE}"
echo

REAL_TAKEOVER="$(env_value_or_default "TCE_ADVISOR_REAL_TAKEOVER" "$REAL_TAKEOVER")"
REAL_TAKEOVER_MODE="$(env_value_or_default "TCE_ADVISOR_REAL_TAKEOVER_MODE" "$REAL_TAKEOVER_MODE")"
ADVISOR_COMMAND="$(env_value_or_default "TCE_ADVISOR_COMMAND" "$ADVISOR_COMMAND")"
ADVISOR_TIMEOUT_SECONDS="$(env_value_or_default "TCE_ADVISOR_TIMEOUT_SECONDS" "$ADVISOR_TIMEOUT_SECONDS")"
ADVISOR_TCE_ENRICH="$(env_value_or_default "TCE_ADVISOR_TCE_ENRICH" "$ADVISOR_TCE_ENRICH")"
ADVISOR_PRIMARY_PROVIDER="$(env_value_or_default "TCE_ADVISOR_PRIMARY_PROVIDER" "$ADVISOR_PRIMARY_PROVIDER")"
ADVISOR_PRIMARY_MODEL="$(env_value_or_default "TCE_ADVISOR_PRIMARY_MODEL" "$ADVISOR_PRIMARY_MODEL")"
ADVISOR_FALLBACK_CHAIN="$(env_value_or_default "TCE_ADVISOR_FALLBACK_CHAIN" "$ADVISOR_FALLBACK_CHAIN")"
ADVISOR_CUSTOM_BASE_URL="$(env_value_or_default "TCE_ADVISOR_CUSTOM_BASE_URL" "$ADVISOR_CUSTOM_BASE_URL")"
ADVISOR_CUSTOM_MODEL="$(env_value_or_default "TCE_ADVISOR_CUSTOM_MODEL" "$ADVISOR_CUSTOM_MODEL")"
ADVISOR_CUSTOM_API_KEY_REF="$(env_value_or_default "TCE_ADVISOR_CUSTOM_API_KEY_REF" "$ADVISOR_CUSTOM_API_KEY_REF")"
ADVISOR_PROVIDER_TIMEOUT_MS="$(env_value_or_default "TCE_ADVISOR_PROVIDER_TIMEOUT_MS" "$ADVISOR_PROVIDER_TIMEOUT_MS")"
ADVISOR_PROVIDER_RETRY_MAX="$(env_value_or_default "TCE_ADVISOR_PROVIDER_RETRY_MAX" "$ADVISOR_PROVIDER_RETRY_MAX")"
EXECUTOR_CLIENTS="$(env_value_or_default "TCE_EXECUTOR_CLIENTS" "$EXECUTOR_CLIENTS")"
normalize_executor_clients "$EXECUTOR_CLIENTS"

if [ "$ACTION" = "install" ]; then
  if [ "$SETUP_MODE_SET_BY_ARG" = "true" ]; then
    echo "Step 1b - Setup mode"
    echo "Setup mode provided by flag: ${SETUP_MODE}"
  elif [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
    echo "Step 1b - Setup mode"
    echo "1) Setup here (default)"
    echo "2) I already configured .env, just start stack"
    read -r -p "Select setup mode [1/2]: " setup_mode_choice
    case "$setup_mode_choice" in
      2|env|ENV|existing|existing_env)
        SETUP_MODE="env"
        ;;
      *)
        SETUP_MODE="setup"
        ;;
    esac
  else
    SETUP_MODE="setup"
  fi
  echo "Selected setup mode: ${SETUP_MODE}"
  echo
fi

if [ "$ACTION" = "install" ] && [ "$SETUP_MODE" = "env" ]; then
  if [ "$STACK_MODE" = "all" ] || [ "$STACK_MODE" = "auto" ]; then
    STACK_MODE="full"
  fi

  if [ ! -f "$ENV_FILE" ]; then
    create_env_choice="Y"
    if [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
      echo ".env not found at ${ENV_FILE}."
      read -r -p "Create it from .env.example and continue? [Y/n]: " create_env_choice
    fi
    case "${create_env_choice:-Y}" in
      n|N|no|NO)
        echo "Aborting. Configure ${ENV_FILE} and rerun install."
        exit 1
        ;;
      *)
        cp "${ROOT}/.env.example" "$ENV_FILE"
        echo "Created ${ENV_FILE} from .env.example"
        ;;
    esac
  fi

  env_operation_mode="$(env_value_or_default "TCE_DEFAULT_OPERATION_MODE" "timeline_only")"
  api_token="$(resolved_api_token_default)"
  workspace_id="$(env_value_or_default "TCE_MCP_WORKSPACE_ID" "personal")"
  workspace_mode="$(env_value_or_default "TCE_MCP_WORKSPACE_MODE" "shared")"
  workspace_map="$(env_value_or_default "TCE_MCP_WORKSPACE_MAP" "")"
  executor_primary_client="$(first_csv_token "$EXECUTOR_CLIENTS")"
  executor_secondary_client="$(second_csv_token "$EXECUTOR_CLIENTS")"
  [ -z "$executor_primary_client" ] && executor_primary_client="codex"
  [ -z "$executor_secondary_client" ] && executor_secondary_client="claude"
  derived_executor_id="${executor_primary_client}-executor"
  derived_secondary_id="${executor_secondary_client}-executor"
  derived_executor_session="${executor_primary_client}"
  derived_secondary_session="${executor_secondary_client}"
  executor_user_id="$(env_value_or_default "TCE_MCP_EXECUTOR_USER_ID" "$derived_executor_id")"
  executor_consumer_id="$(env_value_or_default "TCE_MCP_EXECUTOR_CONSUMER_ID" "$executor_user_id")"
  executor_session_id="$(env_value_or_default "TCE_MCP_EXECUTOR_SESSION_ID" "$derived_executor_session")"
  secondary_user_id="$(env_value_or_default "TCE_MCP_SECONDARY_USER_ID" "$derived_secondary_id")"
  secondary_consumer_id="$(env_value_or_default "TCE_MCP_SECONDARY_CONSUMER_ID" "$secondary_user_id")"
  secondary_session_id="$(env_value_or_default "TCE_MCP_SECONDARY_SESSION_ID" "$derived_secondary_session")"
  identity_map="$(build_identity_map_for_clients "$EXECUTOR_CLIENTS" "$executor_user_id" "$secondary_user_id")"
  consumer_map="$(build_identity_map_for_clients "$EXECUTOR_CLIENTS" "$executor_consumer_id" "$secondary_consumer_id")"
  session_map="$(build_session_map_for_clients "$EXECUTOR_CLIENTS" "$executor_session_id" "$secondary_session_id")"
  default_user_id="${USER:-local-user}"
  user_id="$executor_user_id"
  echo "Launching stack (${STACK_MODE}) using existing .env ..."
  "${ROOT}/scripts/start.sh" "$STACK_MODE" --detach
  configure_maintenance_scheduler_for_stack "$STACK_MODE"
  configure_autonomy_tick_scheduler_for_mode "$env_operation_mode"

  api_port="$(env_value_or_default "TCE_API_PORT" "8080")"
  api_url="http://localhost:${api_port}"
  install_cli_capture_runtime || true
  install_mcp_runtime || true
  configure_mcp_clients_prompt "$api_url" "$api_token" "$workspace_id" "$user_id" "$EXECUTOR_CLIENTS" "$workspace_mode" "$workspace_map" "$identity_map" "$consumer_map" "$session_map"

  echo
  echo "Start complete (env mode)."
  echo "API: http://localhost:${api_port}"
  echo "Mode from .env: ${env_operation_mode}"
  echo "Workspace mode from .env: ${workspace_mode}"
  echo "Executor clients: ${EXECUTOR_CLIENTS}"
  exit 0
fi

if [ "$OPERATION_SET_BY_ARG" = "true" ]; then
  echo "Step 2 - Choose behavior"
  echo "Behavior provided by flag: ${OPERATION_MODE}"
elif [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
  echo "Step 2 - Choose behavior"
  echo "1) Timeline engine only (default)"
  echo "2) Clone advisor (executor + advisor)"
  read -r -p "Select behavior [1/2]: " behavior_choice
  case "$behavior_choice" in
    2|clone|advisor|clone_advisor)
      OPERATION_MODE="clone_advisor"
      ;;
    *)
      OPERATION_MODE="timeline_only"
      ;;
  esac
else
  OPERATION_MODE="$(env_value_or_default "TCE_DEFAULT_OPERATION_MODE" "timeline_only")"
fi
echo "Selected behavior: ${OPERATION_MODE}"
echo

MCP_TOOL_PROFILE="$(env_value_or_default "TCE_MCP_TOOL_PROFILE" "core")"
if [ "$OPERATION_MODE" = "clone_advisor" ]; then
  case "$MCP_TOOL_PROFILE" in
    core|continuity)
      MCP_TOOL_PROFILE="autonomy"
      ;;
  esac
fi
echo "Selected MCP tool profile: ${MCP_TOOL_PROFILE}"
echo

if [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
  echo "Step 2b - Select executor clients"
  echo "Executors are the clients that perform tasks (you can enable multiple)."
  echo "Supported: codex, claude, cursor, lovable"
  read -r -p "Executor clients comma-separated [${EXECUTOR_CLIENTS}]: " executor_clients_input
  normalize_executor_clients "${executor_clients_input:-$EXECUTOR_CLIENTS}"
else
  normalize_executor_clients "$EXECUTOR_CLIENTS"
fi
echo "Selected executors: ${EXECUTOR_CLIENTS}"
if [ "$LOVABLE_SELECTED" = "true" ]; then
  echo "Lovable selected: generic MCP config will be generated for manual import."
fi
echo

workspace_id="$(env_value_or_default "TCE_MCP_WORKSPACE_ID" "personal")"
WORKSPACE_MEMORY_MODE="$(env_value_or_default "TCE_MCP_WORKSPACE_MODE" "$WORKSPACE_MEMORY_MODE")"
WORKSPACE_MAP="$(env_value_or_default "TCE_MCP_WORKSPACE_MAP" "$WORKSPACE_MAP")"
if [ "$ASSUME_YES" != "true" ] && [ -t 0 ]; then
  echo "Step 2c - Workspace memory mode"
  echo "1) Shared workspace memory (default)"
  echo "2) Separate workspace memory per executor"
  echo "Shared = all executors read/write one memory space."
  echo "Separate = each executor gets its own workspace id and isolated memory."
  read -r -p "Select workspace mode [1/2]: " workspace_mode_choice
  case "${workspace_mode_choice:-1}" in
    2|separate|SEPARATE)
      WORKSPACE_MEMORY_MODE="separate"
      ;;
    *)
      WORKSPACE_MEMORY_MODE="shared"
      ;;
  esac

  read -r -p "Base workspace id [${workspace_id}]: " workspace_input
  workspace_id="${workspace_input:-$workspace_id}"
  if [ "$WORKSPACE_MEMORY_MODE" = "separate" ]; then
    default_workspace_map="$(build_workspace_map_for_clients "$workspace_id" "$EXECUTOR_CLIENTS")"
    read -r -p "Per-executor workspace map [${default_workspace_map}]: " workspace_map_input
    WORKSPACE_MAP="${workspace_map_input:-$default_workspace_map}"
  else
    WORKSPACE_MAP=""
  fi
fi
echo "Workspace mode: ${WORKSPACE_MEMORY_MODE}"
if [ "$WORKSPACE_MEMORY_MODE" = "shared" ]; then
  echo "Workspace id: ${workspace_id}"
else
  echo "Workspace map: ${WORKSPACE_MAP}"
fi
echo

if [ "$OPERATION_MODE" = "clone_advisor" ]; then
  if [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
    echo "Step 3 - Choose advisor persona mode"
    echo "1) normal (default)"
    echo "2) naruto"
    echo "3) shadow"
    read -r -p "Select persona mode [1/2/3]: " persona_choice
    case "$persona_choice" in
      2|naruto|Naruto)
        PERSONA_MODE="naruto"
        ;;
      3|shadow|Shadow|shadowmode|shadow_mode)
        PERSONA_MODE="shadow"
        ;;
      *)
        PERSONA_MODE="normal"
        ;;
    esac
    echo "Selected persona mode: ${PERSONA_MODE}"
    echo

    default_activation="$(persona_default_activation "$PERSONA_MODE")"
    default_stop="$(persona_default_stop "$PERSONA_MODE")"
    echo "Step 4 - Persona command style"
    echo "1) Use default commands (recommended)"
    echo "2) Customize commands"
    read -r -p "Select command style [1/2]: " command_style_choice
    case "$command_style_choice" in
      2|custom|Custom)
        PERSONA_COMMAND_STYLE="custom"
        read -r -p "Activation phrase(s) comma-separated [${default_activation}]: " activation_input
        ADVISOR_ACTIVATION_KEYWORD="${activation_input:-$default_activation}"
        read -r -p "Stop phrase [${default_stop}]: " stop_input
        ADVISOR_STOP_KEYWORD="${stop_input:-$default_stop}"
        ;;
      *)
        PERSONA_COMMAND_STYLE="default"
        ADVISOR_ACTIVATION_KEYWORD=""
        ADVISOR_STOP_KEYWORD=""
        ;;
    esac
    echo

    REAL_TAKEOVER="false"
    REAL_TAKEOVER_MODE="suggest"
    ADVISOR_COMMAND=""
    ADVISOR_TIMEOUT_SECONDS="45"
    ADVISOR_TCE_ENRICH="true"
  else
    echo "Step 3 - Persona mode skipped (non-interactive defaults)"
    echo "Step 4 - Persona command style skipped (default commands)"
    PERSONA_MODE="normal"
    PERSONA_COMMAND_STYLE="default"
    ADVISOR_ACTIVATION_KEYWORD=""
    ADVISOR_STOP_KEYWORD=""
    REAL_TAKEOVER="false"
    REAL_TAKEOVER_MODE="suggest"
    ADVISOR_COMMAND=""
    ADVISOR_TIMEOUT_SECONDS="45"
    ADVISOR_TCE_ENRICH="true"
  fi
else
  echo "Step 3 - Persona mode skipped (timeline_only)"
  echo "Step 4 - Persona command style skipped"
fi

if [ "$OPERATION_MODE" = "clone_advisor" ] && [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
  echo "Step 5 - Choose advisor"
  echo "Executors do the work. Choose one advisor model provider."
  echo "Note: model hints below are curated defaults. The script will fetch live provider models before verify."
  echo "Baseline routing enforces category coverage: global + china + custom."
  echo "1) Local Ollama"
  echo "2) OpenAI"
  echo "3) Anthropic"
  echo "4) Gemini"
  echo "5) OpenRouter"
  echo "6) Groq"
  echo "7) Together"
  echo "8) xAI"
  echo "9) DeepSeek (China)"
  echo "10) DashScope / Qwen (China)"
  echo "11) Zhipu GLM (China)"
  echo "12) Moonshot Kimi (China)"
  echo "13) Baidu Qianfan / ERNIE (China)"
  echo "14) Tencent Hunyuan (China)"
  echo "15) Custom hosted (OpenAI-compatible)"
  read -r -p "Select advisor provider [1-15]: " advisor_provider_choice
  ask_provider_api_key="false"
  provider_display=""
  default_model=""
  case "${advisor_provider_choice:-2}" in
    1)
      ADVISOR_PRIMARY_PROVIDER="local_ollama"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ;;
    2)
      ADVISOR_PRIMARY_PROVIDER="openai"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    3)
      ADVISOR_PRIMARY_PROVIDER="anthropic"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    4)
      ADVISOR_PRIMARY_PROVIDER="gemini"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    5)
      ADVISOR_PRIMARY_PROVIDER="openrouter"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    6)
      ADVISOR_PRIMARY_PROVIDER="groq"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    7)
      ADVISOR_PRIMARY_PROVIDER="together"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    8)
      ADVISOR_PRIMARY_PROVIDER="xai"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    9)
      ADVISOR_PRIMARY_PROVIDER="deepseek"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    10)
      ADVISOR_PRIMARY_PROVIDER="dashscope"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    11)
      ADVISOR_PRIMARY_PROVIDER="zhipu"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    12)
      ADVISOR_PRIMARY_PROVIDER="moonshot"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    13)
      ADVISOR_PRIMARY_PROVIDER="qianfan"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    14)
      ADVISOR_PRIMARY_PROVIDER="hunyuan"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    15)
      ADVISOR_PRIMARY_PROVIDER="custom"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model=""
      echo "Custom OpenAI-compatible: examples include vLLM, LM Studio, enterprise gateways."
      read -r -p "Custom OpenAI-compatible base_url: " custom_url_input
      ADVISOR_CUSTOM_BASE_URL="${custom_url_input:-}"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
    *)
      ADVISOR_PRIMARY_PROVIDER="openai"
      provider_display="$(provider_display_name "$ADVISOR_PRIMARY_PROVIDER")"
      default_model="$(provider_default_model "$ADVISOR_PRIMARY_PROVIDER")"
      ADVISOR_FALLBACK_CHAIN="local_ollama"
      ask_provider_api_key="true"
      ;;
  esac
  default_provider_key_ref="$(provider_api_key_ref "$ADVISOR_PRIMARY_PROVIDER")"
  if [ -n "$default_provider_key_ref" ]; then
    ADVISOR_CUSTOM_API_KEY_REF="$default_provider_key_ref"
  fi
  if [ "$ask_provider_api_key" = "true" ]; then
    read -r -p "API key for ${ADVISOR_PRIMARY_PROVIDER}: " provider_api_key_input
    ADVISOR_PROVIDER_API_KEY="${provider_api_key_input:-$ADVISOR_PROVIDER_API_KEY}"
  fi
  if [ "$ADVISOR_PRIMARY_PROVIDER" = "custom" ]; then
    read -r -p "Custom model id: " custom_model_input
    ADVISOR_CUSTOM_MODEL="${custom_model_input:-}"
    ADVISOR_PRIMARY_MODEL="$ADVISOR_CUSTOM_MODEL"
  else
    read -r -p "${provider_display} model [${default_model}]: " provider_model_input
    ADVISOR_PRIMARY_MODEL="${provider_model_input:-$default_model}"
  fi
  read -r -p "Fallback providers comma-separated [default: local_ollama]: " provider_fallback_input
  ADVISOR_FALLBACK_CHAIN="$(ensure_fallback_category_coverage "$ADVISOR_PRIMARY_PROVIDER" "${provider_fallback_input:-local_ollama}")"
  echo "Resolved fallback chain (category-complete): ${ADVISOR_FALLBACK_CHAIN}"
  read -r -p "Change fallback provider model(s)? [y/N]: " fallback_models_choice
  case "${fallback_models_choice:-N}" in
    y|Y|yes|YES)
      ADVISOR_FALLBACK_MODEL_HINTS=""
      IFS=',' read -r -a fallback_parts <<< "${ADVISOR_FALLBACK_CHAIN}"
      for fb_token in "${fallback_parts[@]-}"; do
        fb_provider="$(printf '%s' "$fb_token" | tr '[:upper:]' '[:lower:]' | xargs)"
        if [ -z "$fb_provider" ]; then
          continue
        fi
        fb_display="$(provider_display_name "$fb_provider")"
        fb_default_model="$(provider_default_model "$fb_provider")"
        if [ "$fb_provider" != "$ADVISOR_PRIMARY_PROVIDER" ] && [ "$fb_provider" != "local_ollama" ]; then
          fb_key_env="$(provider_api_env_key "$fb_provider")"
          fb_existing_key=""
          if [ -n "$fb_key_env" ]; then
            fb_existing_key="$(env_value_or_default "$fb_key_env" "")"
          fi
          if [ -z "$fb_existing_key" ]; then
            read -r -p "API key for fallback ${fb_display} (${fb_provider}) [required if this fallback is used]: " fb_key_input
            if [ -n "${fb_key_input:-}" ] && [ -n "$fb_key_env" ]; then
              append_env_override "$fb_key_env" "$fb_key_input"
            fi
          fi
        fi
        if [ -n "$fb_default_model" ]; then
          read -r -p "Fallback model for ${fb_display} (${fb_provider}) [${fb_default_model}]: " fb_model_input
          fb_model="${fb_model_input:-$fb_default_model}"
        else
          read -r -p "Fallback model for ${fb_display} (${fb_provider}): " fb_model
        fi
        if [ -n "${fb_model:-}" ]; then
          ADVISOR_FALLBACK_MODEL_HINTS="${ADVISOR_FALLBACK_MODEL_HINTS:+${ADVISOR_FALLBACK_MODEL_HINTS},}${fb_provider}:${fb_model}"
        fi
      done
      ;;
    *)
      ADVISOR_FALLBACK_MODEL_HINTS=""
      ;;
  esac
  if [ "$ADVISOR_PRIMARY_PROVIDER" = "local_ollama" ]; then
    ADVISOR_CUSTOM_MODEL=""
  fi
  read -r -p "Advisor provider timeout ms [6000]: " provider_timeout_input
  if [ -n "${provider_timeout_input:-}" ] && printf '%s' "$provider_timeout_input" | grep -Eq '^[0-9]+$'; then
    ADVISOR_PROVIDER_TIMEOUT_MS="$provider_timeout_input"
  fi
  read -r -p "Advisor provider retry max [2]: " provider_retry_input
  if [ -n "${provider_retry_input:-}" ] && printf '%s' "$provider_retry_input" | grep -Eq '^[0-9]+$'; then
    ADVISOR_PROVIDER_RETRY_MAX="$provider_retry_input"
  fi
  echo
fi

clone_turns_default="$(env_value_or_default "TCE_CLONE_MAX_TURNS_PER_INTERACTION" "$CLONE_MAX_TURNS_PER_INTERACTION")"
CLONE_MAX_TURNS_PER_INTERACTION="$clone_turns_default"
if [ "$OPERATION_MODE" = "clone_advisor" ] && [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
  echo "Step 7 - Advisor loop guard"
  echo "Default max advisor turns per interaction: ${clone_turns_default}"
  read -r -p "Change advisor loop limit? [y/N]: " clone_turns_change_choice
  case "${clone_turns_change_choice:-N}" in
    y|Y|yes|YES)
      read -r -p "Max advisor turns per interaction [${clone_turns_default}]: " clone_turns_input
      if [ -n "${clone_turns_input:-}" ]; then
        if printf '%s' "$clone_turns_input" | grep -Eq '^[0-9]+$' && [ "$clone_turns_input" -ge 1 ] && [ "$clone_turns_input" -le 200 ]; then
          CLONE_MAX_TURNS_PER_INTERACTION="$clone_turns_input"
        else
          echo "Invalid turn limit; using default ${clone_turns_default}."
          CLONE_MAX_TURNS_PER_INTERACTION="$clone_turns_default"
        fi
      fi
      ;;
    *)
      CLONE_MAX_TURNS_PER_INTERACTION="$clone_turns_default"
      ;;
  esac
fi

takeover_safety_default="$(env_value_or_default "TCE_TAKEOVER_SAFETY_POLICY" "$TAKEOVER_SAFETY_POLICY")"
takeover_confirm_default="$(env_value_or_default "TCE_TAKEOVER_CONFIRM_KEYWORD" "$TAKEOVER_CONFIRM_KEYWORD")"
takeover_deny_default="$(env_value_or_default "TCE_TAKEOVER_DENY_KEYWORD" "$TAKEOVER_DENY_KEYWORD")"
TAKEOVER_SAFETY_POLICY="$takeover_safety_default"
TAKEOVER_CONFIRM_KEYWORD="$takeover_confirm_default"
TAKEOVER_DENY_KEYWORD="$takeover_deny_default"
if [ "$OPERATION_MODE" = "clone_advisor" ] && [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
  echo "Step 8 - Takeover safety policy"
  read -r -p "Enable high-risk pause safety gate? [Y/n]: " safety_choice
  case "${safety_choice:-Y}" in
    n|N|no|NO)
      TAKEOVER_SAFETY_POLICY="none"
      ;;
    *)
      TAKEOVER_SAFETY_POLICY="high-risk-pause"
      read -r -p "Set custom confirm keyword? [y/N]: " confirm_custom_choice
      case "${confirm_custom_choice:-N}" in
        y|Y|yes|YES)
          read -r -p "Confirm keyword [${takeover_confirm_default}]: " confirm_keyword_input
          TAKEOVER_CONFIRM_KEYWORD="${confirm_keyword_input:-$takeover_confirm_default}"
          ;;
        *)
          TAKEOVER_CONFIRM_KEYWORD="$takeover_confirm_default"
          ;;
      esac
      read -r -p "Set custom deny keyword? [y/N]: " deny_custom_choice
      case "${deny_custom_choice:-N}" in
        y|Y|yes|YES)
          read -r -p "Deny keyword [${takeover_deny_default}]: " deny_keyword_input
          TAKEOVER_DENY_KEYWORD="${deny_keyword_input:-$takeover_deny_default}"
          ;;
        *)
          TAKEOVER_DENY_KEYWORD="$takeover_deny_default"
          ;;
      esac
      ;;
  esac
fi

api_token_default="$(resolved_api_token_default)"
api_token="$api_token_default"
if [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
  echo "Step 9 - Local API token"
  read -r -p "Set a custom local API token? [y/N]: " api_token_custom_choice
  case "${api_token_custom_choice:-N}" in
    y|Y|yes|YES)
      read -r -p "Enter custom local API token: " api_token_input
      if [ -n "${api_token_input:-}" ]; then
        api_token="$api_token_input"
      else
        echo "Empty token provided; using default token."
        api_token="$api_token_default"
      fi
      ;;
    *)
      api_token="$api_token_default"
      ;;
  esac
fi

auto_capture_default="$(env_value_or_default "TCE_AUTO_CAPTURE_INTERACTIONS" "$AUTO_CAPTURE_INTERACTIONS")"
auto_capture_skip_default="$(env_value_or_default "TCE_AUTO_CAPTURE_SKIP_SENSITIVE" "$AUTO_CAPTURE_SKIP_SENSITIVE")"
auto_capture_max_default="$(env_value_or_default "TCE_AUTO_CAPTURE_MAX_CHARS" "$AUTO_CAPTURE_MAX_CHARS")"
AUTO_CAPTURE_INTERACTIONS="$auto_capture_default"
AUTO_CAPTURE_SKIP_SENSITIVE="$auto_capture_skip_default"
AUTO_CAPTURE_MAX_CHARS="$auto_capture_max_default"

if [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
  echo "Step 10 - Auto-capture behavior"
  echo "Capture search/context/advisor interaction events?"
  read -r -p "Enable auto-capture [Y/n]: " capture_choice
  case "${capture_choice:-Y}" in
    n|N|no|NO)
      AUTO_CAPTURE_INTERACTIONS="false"
      ;;
    *)
      AUTO_CAPTURE_INTERACTIONS="true"
      ;;
  esac

  if [ "$AUTO_CAPTURE_INTERACTIONS" = "true" ]; then
    read -r -p "Skip sensitive content automatically [Y/n]: " skip_sensitive_choice
    case "${skip_sensitive_choice:-Y}" in
      n|N|no|NO)
        AUTO_CAPTURE_SKIP_SENSITIVE="false"
        ;;
      *)
        AUTO_CAPTURE_SKIP_SENSITIVE="true"
        ;;
    esac

    read -r -p "Max captured chars per interaction [${auto_capture_max_default}]: " max_chars_input
    if [ -n "${max_chars_input:-}" ]; then
      if printf '%s' "$max_chars_input" | grep -Eq '^[0-9]+$'; then
        AUTO_CAPTURE_MAX_CHARS="$max_chars_input"
      else
        echo "Invalid max chars; using default ${auto_capture_max_default}."
        AUTO_CAPTURE_MAX_CHARS="$auto_capture_max_default"
      fi
    fi
  fi
fi

if [ "$OPERATION_MODE" = "clone_advisor" ]; then
  if [ "$ASSUME_YES" = "true" ]; then
    RUN_TAKEOVER_SELF_TEST="true"
  elif [ -t 0 ]; then
    echo "Step 11 - Takeover self-test"
    read -r -p "Run takeover self-test after setup? [y/N]: " self_test_choice
    case "${self_test_choice:-N}" in
      y|Y|yes|YES)
        RUN_TAKEOVER_SELF_TEST="true"
        ;;
      *)
        RUN_TAKEOVER_SELF_TEST="false"
        ;;
    esac
  fi
fi

executor_primary_client="$(first_csv_token "$EXECUTOR_CLIENTS")"
if [ -z "$executor_primary_client" ]; then
  executor_primary_client="codex"
fi
executor_secondary_client="$(second_csv_token "$EXECUTOR_CLIENTS")"
if [ -z "$executor_secondary_client" ]; then
  executor_secondary_client="claude"
fi
derived_executor_id="${executor_primary_client}-executor"
derived_advisor_id="${executor_secondary_client}-executor"
derived_executor_session="${executor_primary_client}"
derived_advisor_session="${executor_secondary_client}"

if [ "$OPERATION_MODE" = "clone_advisor" ]; then
  EXECUTOR_USER_ID="$derived_executor_id"
  EXECUTOR_CONSUMER_ID="$derived_executor_id"
  EXECUTOR_SESSION_ID="$derived_executor_session"
  ADVISOR_USER_ID="$derived_advisor_id"
  ADVISOR_CONSUMER_ID="$derived_advisor_id"
  ADVISOR_SESSION_ID="$derived_advisor_session"
else
  EXECUTOR_USER_ID="$(env_value_or_default "TCE_MCP_EXECUTOR_USER_ID" "$derived_executor_id")"
  EXECUTOR_CONSUMER_ID="$(env_value_or_default "TCE_MCP_EXECUTOR_CONSUMER_ID" "$EXECUTOR_USER_ID")"
  EXECUTOR_SESSION_ID="$(env_value_or_default "TCE_MCP_EXECUTOR_SESSION_ID" "$derived_executor_session")"
  ADVISOR_USER_ID="$(env_value_or_default "TCE_MCP_SECONDARY_USER_ID" "$derived_advisor_id")"
  ADVISOR_CONSUMER_ID="$(env_value_or_default "TCE_MCP_SECONDARY_CONSUMER_ID" "$ADVISOR_USER_ID")"
  ADVISOR_SESSION_ID="$(env_value_or_default "TCE_MCP_SECONDARY_SESSION_ID" "$derived_advisor_session")"
fi

backup_env

resolved_ollama_base_url="$(env_value_or_default "TCE_ADVISOR_LOCAL_OLLAMA_BASE_URL" "$(default_local_ollama_base_url)")"
resolved_lmstudio_base_url="$(env_value_or_default "TCE_ADVISOR_LOCAL_LMSTUDIO_BASE_URL" "$(default_local_lmstudio_base_url)")"
resolved_primary_key_ref="${ADVISOR_CUSTOM_API_KEY_REF:-$(provider_api_key_ref "$ADVISOR_PRIMARY_PROVIDER")}"
advisor_routes_json="$(build_advisor_routes_json "$ADVISOR_PRIMARY_PROVIDER" "$ADVISOR_PRIMARY_MODEL" "$ADVISOR_CUSTOM_BASE_URL" "$resolved_primary_key_ref" "$ADVISOR_FALLBACK_CHAIN" "$ADVISOR_FALLBACK_MODEL_HINTS")"

set_env_key "TCE_API_TOKEN" "$api_token"
set_env_key "TCE_API_TOKENS" "$api_token"
set_env_key "TCE_DEFAULT_OPERATION_MODE" "$OPERATION_MODE"
set_env_key "TCE_MCP_TOOL_PROFILE" "$MCP_TOOL_PROFILE"
set_env_key "TCE_ADVISOR_PERSONA_MODE" "$PERSONA_MODE"
set_env_key "TCE_ADVISOR_REAL_TAKEOVER" "$REAL_TAKEOVER"
set_env_key "TCE_ADVISOR_REAL_TAKEOVER_MODE" "$REAL_TAKEOVER_MODE"
set_env_key "TCE_CLONE_MAX_TURNS_PER_INTERACTION" "$CLONE_MAX_TURNS_PER_INTERACTION"
set_env_key "TCE_ADVISOR_COMMAND" "$ADVISOR_COMMAND"
set_env_key "TCE_ADVISOR_TIMEOUT_SECONDS" "$ADVISOR_TIMEOUT_SECONDS"
set_env_key "TCE_ADVISOR_TCE_ENRICH" "$ADVISOR_TCE_ENRICH"
set_env_key "TCE_ADVISOR_ACTIVATION_KEYWORD" "$ADVISOR_ACTIVATION_KEYWORD"
set_env_key "TCE_ADVISOR_STOP_KEYWORD" "$ADVISOR_STOP_KEYWORD"
set_env_key "TCE_ADVISOR_PRIMARY_PROVIDER" "$ADVISOR_PRIMARY_PROVIDER"
set_env_key "TCE_ADVISOR_PRIMARY_MODEL" "$ADVISOR_PRIMARY_MODEL"
set_env_key "TCE_ADVISOR_FALLBACK_CHAIN" "$ADVISOR_FALLBACK_CHAIN"
set_env_key "TCE_ADVISOR_CUSTOM_BASE_URL" "$ADVISOR_CUSTOM_BASE_URL"
set_env_key "TCE_ADVISOR_CUSTOM_MODEL" "$ADVISOR_CUSTOM_MODEL"
set_env_key "TCE_ADVISOR_CUSTOM_API_KEY_REF" "$ADVISOR_CUSTOM_API_KEY_REF"
set_env_key "TCE_ADVISOR_PROVIDER_TIMEOUT_MS" "$ADVISOR_PROVIDER_TIMEOUT_MS"
set_env_key "TCE_ADVISOR_PROVIDER_RETRY_MAX" "$ADVISOR_PROVIDER_RETRY_MAX"
set_env_key "TCE_ADVISOR_FALLBACK_MODEL_HINTS" "$ADVISOR_FALLBACK_MODEL_HINTS"
set_env_key "TCE_ADVISOR_ROUTES_JSON" "$advisor_routes_json"
set_env_key "TCE_ADVISOR_ACTIVE_PROFILE_ID" "default"
set_env_key "TCE_ADVISOR_LOCAL_OLLAMA_BASE_URL" "$resolved_ollama_base_url"
set_env_key "TCE_ADVISOR_LOCAL_LMSTUDIO_BASE_URL" "$resolved_lmstudio_base_url"
set_env_key "TCE_EXECUTOR_CLIENTS" "$EXECUTOR_CLIENTS"
set_env_key "TCE_MCP_EXECUTOR_USER_ID" "$EXECUTOR_USER_ID"
set_env_key "TCE_MCP_EXECUTOR_CONSUMER_ID" "$EXECUTOR_CONSUMER_ID"
set_env_key "TCE_MCP_EXECUTOR_SESSION_ID" "$EXECUTOR_SESSION_ID"
set_env_key "TCE_MCP_SECONDARY_USER_ID" "$ADVISOR_USER_ID"
set_env_key "TCE_MCP_SECONDARY_CONSUMER_ID" "$ADVISOR_CONSUMER_ID"
set_env_key "TCE_MCP_SECONDARY_SESSION_ID" "$ADVISOR_SESSION_ID"
set_env_key "TCE_ADVISOR_USER_ID" "$ADVISOR_USER_ID"
set_env_key "TCE_ADVISOR_CONSUMER_ID" "$ADVISOR_CONSUMER_ID"
set_env_key "TCE_MCP_SESSION_ID" "$EXECUTOR_SESSION_ID"
set_env_key "TCE_MCP_WORKSPACE_ID" "$workspace_id"
set_env_key "TCE_MCP_WORKSPACE_MODE" "$WORKSPACE_MEMORY_MODE"
set_env_key "TCE_MCP_WORKSPACE_MAP" "$WORKSPACE_MAP"
set_env_key "TCE_AUTO_CAPTURE_INTERACTIONS" "$AUTO_CAPTURE_INTERACTIONS"
set_env_key "TCE_AUTO_CAPTURE_SKIP_SENSITIVE" "$AUTO_CAPTURE_SKIP_SENSITIVE"
set_env_key "TCE_AUTO_CAPTURE_MAX_CHARS" "$AUTO_CAPTURE_MAX_CHARS"
set_env_key "TCE_TAKEOVER_SAFETY_POLICY" "$TAKEOVER_SAFETY_POLICY"
set_env_key "TCE_TAKEOVER_CONFIRM_KEYWORD" "$TAKEOVER_CONFIRM_KEYWORD"
set_env_key "TCE_TAKEOVER_DENY_KEYWORD" "$TAKEOVER_DENY_KEYWORD"
primary_api_env_key="$(provider_api_env_key "$ADVISOR_PRIMARY_PROVIDER")"
if [ -n "$primary_api_env_key" ] && [ -n "${ADVISOR_PROVIDER_API_KEY:-}" ]; then
  set_env_key "$primary_api_env_key" "$ADVISOR_PROVIDER_API_KEY"
fi
if [ -n "${ADVISOR_ENV_KEY_OVERRIDES:-}" ]; then
  while IFS='=' read -r env_key env_value; do
    if [ -z "${env_key:-}" ] || [ -z "${env_value:-}" ]; then
      continue
    fi
    set_env_key "$env_key" "$env_value"
  done <<< "$ADVISOR_ENV_KEY_OVERRIDES"
fi

echo
echo "Launching stack (${STACK_MODE}) ..."
"${ROOT}/scripts/start.sh" "$STACK_MODE" --detach
ensure_qdrant_ready_for_stack "$STACK_MODE" || true
configure_maintenance_scheduler_for_stack "$STACK_MODE"
configure_autonomy_tick_scheduler_for_mode "$OPERATION_MODE"

api_port="$(env_value_or_default "TCE_API_PORT" "8080")"
api_url="http://localhost:${api_port}"
workspace_id="$(env_value_or_default "TCE_MCP_WORKSPACE_ID" "personal")"
workspace_mode="$(env_value_or_default "TCE_MCP_WORKSPACE_MODE" "shared")"
workspace_map="$(env_value_or_default "TCE_MCP_WORKSPACE_MAP" "")"
default_user_id="${USER:-local-user}"
user_id="$(env_value_or_default "TCE_MCP_EXECUTOR_USER_ID" "$(env_value_or_default "TCE_MCP_USER_ID" "$default_user_id")")"
db_url="$(env_value_or_default "TCE_DATABASE_URL" "postgresql+psycopg://postgres:postgres@localhost:5432/tce")"

if command -v curl >/dev/null 2>&1; then
  for _ in $(seq 1 60); do
    if curl -fsS "${api_url}/v1/health" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  TCE_API_TOKEN="$api_token" "${ROOT}/scripts/set_mode.sh" "$OPERATION_MODE" >/dev/null 2>&1 || true
fi

if [ "$OPERATION_MODE" = "clone_advisor" ] && command -v curl >/dev/null 2>&1; then
  primary_model="$ADVISOR_PRIMARY_MODEL"
  if [ "$ADVISOR_PRIMARY_PROVIDER" = "custom" ] && [ -n "$ADVISOR_CUSTOM_MODEL" ]; then
    primary_model="$ADVISOR_CUSTOM_MODEL"
  fi
  if [ "$ASSUME_YES" != "true" ] && [ -t 0 ] && [ "$ADVISOR_PRIMARY_PROVIDER" != "custom" ]; then
    models_resp="$(curl -sS -X GET "${api_url}/v1/setup/advisor/models?provider=${ADVISOR_PRIMARY_PROVIDER}" \
      -H "Authorization: Bearer ${api_token}" \
      -H "X-TCE-Workspace: ${workspace_id}" \
      -H "X-TCE-User: ${user_id}" || true)"
    models_rendered="$(python3 -c '
import json, sys
raw = sys.argv[1]
try:
    obj = json.loads(raw)
except Exception:
    print("")
    raise SystemExit(0)
models = obj.get("models") or []
if not isinstance(models, list):
    print("")
    raise SystemExit(0)
models = [str(m).strip() for m in models if str(m).strip()]
if not models:
    print("")
    raise SystemExit(0)
print("\n".join(models[:20]))
' "$models_resp" 2>/dev/null || true)"
    if [ -n "$models_rendered" ]; then
      echo "Live model catalog (${ADVISOR_PRIMARY_PROVIDER}):"
      while IFS= read -r model_line; do
        echo "  - ${model_line}"
      done <<< "$models_rendered"
      read -r -p "Advisor model [${primary_model}]: " live_model_input
      if [ -n "${live_model_input:-}" ]; then
        primary_model="$live_model_input"
        ADVISOR_PRIMARY_MODEL="$live_model_input"
        if [ "$ADVISOR_PRIMARY_PROVIDER" = "custom" ]; then
          ADVISOR_CUSTOM_MODEL="$live_model_input"
        fi
      fi
    fi
  fi
  resolved_primary_key_ref="${ADVISOR_CUSTOM_API_KEY_REF:-$(provider_api_key_ref "$ADVISOR_PRIMARY_PROVIDER")}"
  routes_json="$(build_advisor_routes_json "$ADVISOR_PRIMARY_PROVIDER" "$primary_model" "$ADVISOR_CUSTOM_BASE_URL" "$resolved_primary_key_ref" "$ADVISOR_FALLBACK_CHAIN" "$ADVISOR_FALLBACK_MODEL_HINTS")"
  fallback_json="$(csv_to_json_array "$ADVISOR_FALLBACK_CHAIN")"
  set_env_key "TCE_ADVISOR_PRIMARY_MODEL" "$primary_model"
  if [ "$ADVISOR_PRIMARY_PROVIDER" = "custom" ]; then
    set_env_key "TCE_ADVISOR_CUSTOM_MODEL" "$primary_model"
  fi
  set_env_key "TCE_ADVISOR_ROUTES_JSON" "$routes_json"
  set_env_key "TCE_ADVISOR_ACTIVE_PROFILE_ID" "default"
  primary_api_env_key="$(provider_api_env_key "$ADVISOR_PRIMARY_PROVIDER")"
  if [ -n "$primary_api_env_key" ] && [ -n "${ADVISOR_PROVIDER_API_KEY:-}" ]; then
    set_env_key "$primary_api_env_key" "$ADVISOR_PROVIDER_API_KEY"
  fi
  if [ "$ADVISOR_PRIMARY_PROVIDER" = "local_ollama" ]; then
    echo "Preparing local advisor model '${primary_model}' ..."
    local_model_ready="false"
    if docker compose -f "${ROOT}/infra/docker-compose.yml" ps --services --filter status=running 2>/dev/null | grep -qx "ollama"; then
      if docker compose -f "${ROOT}/infra/docker-compose.yml" exec -T ollama ollama pull "$primary_model" >/dev/null 2>&1; then
        local_model_ready="true"
      fi
    fi
    if [ "$local_model_ready" != "true" ] && command -v ollama >/dev/null 2>&1; then
      if ollama pull "$primary_model" >/dev/null 2>&1; then
        local_model_ready="true"
      fi
    fi
    if [ "$local_model_ready" != "true" ]; then
      echo "Failed to pull local model '${primary_model}'. Install/enable Ollama and retry."
      exit 1
    fi
  fi
  verify_api_key_json="null"
  if [ -n "${ADVISOR_PROVIDER_API_KEY:-}" ]; then
    verify_api_key_json="\"${ADVISOR_PROVIDER_API_KEY}\""
  fi
  verify_payload=$(
    cat <<JSON
{"provider_id":"${ADVISOR_PRIMARY_PROVIDER}","model":"${primary_model}","base_url":"${ADVISOR_CUSTOM_BASE_URL}","api_key":${verify_api_key_json},"api_key_ref":"${ADVISOR_CUSTOM_API_KEY_REF}","fallback_chain":${fallback_json},"timeout_ms":${ADVISOR_PROVIDER_TIMEOUT_MS},"retry_max":${ADVISOR_PROVIDER_RETRY_MAX}}
JSON
  )
  verify_resp="$(curl -sS -X POST "${api_url}/v1/setup/advisor/verify" \
    -H "Authorization: Bearer ${api_token}" \
    -H "Content-Type: application/json" \
    -H "X-TCE-Workspace: ${workspace_id}" \
    -H "X-TCE-User: ${user_id}" \
    -d "$verify_payload" || true)"
  verify_ok="false"
  if printf '%s' "$verify_resp" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'; then
    verify_ok="true"
  fi
  if [ "$verify_ok" != "true" ]; then
    echo "Advisor verify failed: ${verify_resp}"
    if [ "$ASSUME_YES" != "true" ]; then
      echo "Fix provider/model/key and rerun install."
      exit 1
    fi
  fi

  config_payload=$(
    cat <<JSON
{"advisor_primary_provider":"${ADVISOR_PRIMARY_PROVIDER}","advisor_primary_model":"${primary_model}","advisor_fallback_chain":${fallback_json},"advisor_custom_base_url":"${ADVISOR_CUSTOM_BASE_URL}","advisor_custom_model":"${ADVISOR_CUSTOM_MODEL}","advisor_custom_api_key_ref":"${ADVISOR_CUSTOM_API_KEY_REF}","advisor_provider_timeout_ms":${ADVISOR_PROVIDER_TIMEOUT_MS},"advisor_provider_retry_max":${ADVISOR_PROVIDER_RETRY_MAX},"profile_id":"default","routing_mode":"adaptive","failure_policy":"risk_aware_fail_safe","routes":${routes_json},"api_key":${verify_api_key_json}}
JSON
  )
  curl -sS -X PUT "${api_url}/v1/setup/advisor/config" \
    -H "Authorization: Bearer ${api_token}" \
    -H "Content-Type: application/json" \
    -H "X-TCE-Workspace: ${workspace_id}" \
    -H "X-TCE-User: ${user_id}" \
    -d "$config_payload" >/dev/null || true
fi

install_cli_capture_runtime || true
install_mcp_runtime || true
CAPTURE_CMD="$(capture_cli_command_hint)"
MCP_CMD="$(mcp_server_command_hint)"

bootstrap_timeline_data "$api_url" "$api_token" "$workspace_id" "$user_id" "$db_url"
identity_map="$(build_identity_map_for_clients "$EXECUTOR_CLIENTS" "$EXECUTOR_USER_ID" "$ADVISOR_USER_ID")"
consumer_map="$(build_identity_map_for_clients "$EXECUTOR_CLIENTS" "$EXECUTOR_CONSUMER_ID" "$ADVISOR_CONSUMER_ID")"
session_map="$(build_session_map_for_clients "$EXECUTOR_CLIENTS" "$EXECUTOR_SESSION_ID" "$ADVISOR_SESSION_ID")"
configure_mcp_clients_prompt "$api_url" "$api_token" "$workspace_id" "$user_id" "$EXECUTOR_CLIENTS" "$workspace_mode" "$workspace_map" "$identity_map" "$consumer_map" "$session_map"
if [ "$OPERATION_MODE" = "clone_advisor" ]; then
  apply_takeover_policy_prompt
  ensure_workspace_takeover_policy "${PWD}" "$OPERATION_MODE" || true
fi

if [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
  read -r -p "Install auto-takeover client hooks? [Y/n]: " hooks_choice
  case "${hooks_choice:-Y}" in
    n|N|no|NO) ;;
    *) install_client_hooks "${PWD}" || true ;;
  esac
elif [ "$ASSUME_YES" = "true" ]; then
  install_client_hooks "${PWD}" || true
fi

if [ "$RUN_TAKEOVER_SELF_TEST" = "true" ] && [ "$OPERATION_MODE" = "clone_advisor" ]; then
  echo
  echo "Running takeover self-test..."
  self_test_failed="false"
  activation_phrase="$(persona_default_activation "$PERSONA_MODE")"
  if [ -x "$LOCAL_CAPTURE_LAUNCHER" ]; then
    if "$LOCAL_CAPTURE_LAUNCHER" advisor-chat "$activation_phrase" --session-id install-self-test --persona-mode "$PERSONA_MODE" >/dev/null 2>&1; then
      echo "Takeover self-test: PASS"
    else
      echo "Takeover self-test: FAIL (run manually with ${LOCAL_CAPTURE_LAUNCHER} advisor-chat \"${activation_phrase}\")"
      self_test_failed="true"
    fi
  elif command -v tce-capture >/dev/null 2>&1; then
    if tce-capture advisor-chat "$activation_phrase" --session-id install-self-test --persona-mode "$PERSONA_MODE" >/dev/null 2>&1; then
      echo "Takeover self-test: PASS"
    else
      echo "Takeover self-test: FAIL (run manually with tce-capture advisor-chat \"${activation_phrase}\")"
      self_test_failed="true"
    fi
  else
    echo "Takeover self-test skipped: tce-capture runtime not available."
    self_test_failed="true"
  fi

  workspace_agents="${PWD}/AGENTS.md"
  workspace_claude="${PWD}/CLAUDE.md"
  policy_active="false"
  if [ -f "$workspace_agents" ] && grep -Fq "tce.takeover_step" "$workspace_agents"; then
    policy_active="true"
  fi
  if [ -f "$workspace_claude" ] && grep -Fq "tce.takeover_step" "$workspace_claude"; then
    policy_active="true"
  fi
  if [ "$policy_active" != "true" ]; then
    echo "Takeover policy self-test: FAIL (workspace policy not active in ${PWD})"
    echo "Run: ./scripts/apply_takeover_policy.sh --workspace-root \"${PWD}\" --copy-policy --open-claude"
    self_test_failed="true"
  else
    echo "Takeover policy self-test: PASS"
  fi

  if [ "$self_test_failed" = "true" ]; then
    echo "Takeover self-test failed. Fix the items above and re-run setup."
    exit 1
  fi
fi

cleanup_env_backups
trap - ERR

echo
echo "Setup complete."
echo "API: ${api_url}"
echo "Mode: ${OPERATION_MODE}"
echo "Persona mode: ${PERSONA_MODE}"
echo "Real takeover: ${REAL_TAKEOVER} (mode=${REAL_TAKEOVER_MODE})"
echo "Takeover safety policy: ${TAKEOVER_SAFETY_POLICY} (confirm='${TAKEOVER_CONFIRM_KEYWORD}', deny='${TAKEOVER_DENY_KEYWORD}')"
echo "Clone max turns per interaction: ${CLONE_MAX_TURNS_PER_INTERACTION}"
if [ "$OPERATION_MODE" = "clone_advisor" ]; then
  echo "Executor clients: ${EXECUTOR_CLIENTS}"
  echo "Advisor provider: ${ADVISOR_PRIMARY_PROVIDER} (model='${ADVISOR_PRIMARY_MODEL}')"
  echo "Advisor fallback chain: ${ADVISOR_FALLBACK_CHAIN}"
  if [ "$LOVABLE_SELECTED" = "true" ]; then
    echo "Lovable setup: import generated generic MCP config from docs/mcp-config/generated/generic_mcp.json"
  fi
fi
echo "Auto-capture: ${AUTO_CAPTURE_INTERACTIONS} (skip-sensitive=${AUTO_CAPTURE_SKIP_SENSITIVE}, max-chars=${AUTO_CAPTURE_MAX_CHARS})"
echo "Takeover auto-call policy files: ${ROOT}/AGENTS.md and ${ROOT}/CLAUDE.md"
if [ "$OPERATION_MODE" = "clone_advisor" ]; then
  if workspace_takeover_policy_active "${PWD}"; then
    echo "Workspace takeover policy: active (${PWD})"
  else
    echo "Workspace takeover policy: missing (${PWD})"
    echo "Apply with: ./scripts/apply_takeover_policy.sh --workspace-root \"${PWD}\""
  fi
fi
echo "Self-heal: ${ROOT}/scripts/self-heal.sh"
if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -Fq "${MAINTENANCE_CRON_MARKER}"; then
  echo "Lifecycle scheduler: configured (weekly cron)"
else
  echo "Lifecycle scheduler: not configured"
fi
if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -Fq "${AUTONOMY_TICK_CRON_MARKER}"; then
  echo "Autonomy tick scheduler: configured (5-minute cron)"
else
  echo "Autonomy tick scheduler: not configured"
fi
if [ -f "${PWD}/.claude/settings.json" ]; then
  echo "Client hooks: ${PWD}/.claude/settings.json (UserPromptSubmit + PostToolUse)"
fi
echo "Install log: ${INSTALL_LOG}"
if [ "$STACK_MODE" = "full" ]; then
  echo "Prometheus: http://localhost:9090"
  echo "Grafana: http://localhost:3000"
fi
echo
echo "Recommended next checks:"
echo "  - Run diagnostics: ./scripts/doctor.sh ${STACK_MODE}"
echo "  - MCP launcher: ${MCP_CMD}"
echo "  - Capture note: ${CAPTURE_CMD} note \"timeline check\" --tags check"
echo "  - Backup stack data (Postgres + Qdrant): ./scripts/install.sh backup"
echo "  - Restore stack data: ./scripts/install.sh restore"
echo
echo "Next:"
echo "  - Use timeline only: ./scripts/set_mode.sh timeline_only"
echo "  - Use clone advisor: ./scripts/set_mode.sh clone_advisor"
echo "  - Reassign AI roles: ./scripts/assign_ai_roles.sh"
echo "  - Configure MCP clients: ./scripts/configure_mcp_clients.sh --client ${EXECUTOR_CLIENTS}"
echo
echo "IMPORTANT: Restart your executor clients (Claude Desktop, Codex Desktop, Cursor) to pick up the new MCP configuration."
