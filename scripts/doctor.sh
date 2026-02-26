#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FULL_COMPOSE="$ROOT/infra/docker-compose.yml"
LITE_COMPOSE="$ROOT/infra/docker-compose.lite.yml"
AUTONOMY_TICK_CRON_MARKER="# open-timeline-engine-autonomy-tick"
MODE="auto"

usage() {
  cat <<'EOF'
Usage: ./scripts/doctor.sh [full|lite|all|auto]

Examples:
  ./scripts/doctor.sh
  ./scripts/doctor.sh full
  ./scripts/doctor.sh auto
EOF
}

if [ $# -gt 1 ]; then
  usage
  exit 1
fi

if [ $# -eq 1 ]; then
  case "$1" in
    full|lite|all|auto)
      MODE="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown mode: $1" >&2
      usage
      exit 1
      ;;
  esac
fi

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

pass() {
  echo "[PASS] $1"
  PASS_COUNT=$((PASS_COUNT + 1))
}

fail() {
  echo "[FAIL] $1"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

warn() {
  echo "[WARN] $1"
  WARN_COUNT=$((WARN_COUNT + 1))
}

if command -v docker >/dev/null 2>&1; then
  pass "docker available"
else
  fail "docker not found"
fi

if docker compose version >/dev/null 2>&1; then
  pass "docker compose available"
else
  fail "docker compose not available"
fi

API_URL="${TCE_API_BASE_URL:-http://localhost:8080}"
if [ -f "$ROOT/.env" ]; then
  TOKEN_FROM_ENV="$(awk -F= '$1=="TCE_API_TOKEN" {print $2}' "$ROOT/.env" | tail -n1)"
  TOKEN_FROM_LIST="$(awk -F= '$1=="TCE_API_TOKENS" {print $2}' "$ROOT/.env" | tail -n1)"
else
  TOKEN_FROM_ENV=""
  TOKEN_FROM_LIST=""
fi

if [ -n "${TCE_API_TOKEN:-}" ]; then
  TOKEN="$TCE_API_TOKEN"
elif [ -n "$TOKEN_FROM_ENV" ]; then
  TOKEN="$TOKEN_FROM_ENV"
elif [ -n "$TOKEN_FROM_LIST" ]; then
  TOKEN="${TOKEN_FROM_LIST%%,*}"
else
  TOKEN="local-dev-token"
fi

if command -v curl >/dev/null 2>&1; then
  if curl -fsS "${API_URL}/v1/health" >/dev/null 2>&1; then
    pass "API health endpoint reachable (${API_URL}/v1/health)"
  else
    fail "API health endpoint unreachable (${API_URL}/v1/health)"
  fi

  if curl -fsS -H "Authorization: Bearer ${TOKEN}" "${API_URL}/v1/runtime/mode" >/dev/null 2>&1; then
    pass "runtime mode endpoint reachable (${API_URL}/v1/runtime/mode)"
  else
    fail "runtime mode endpoint unreachable (${API_URL}/v1/runtime/mode)"
  fi
else
  fail "curl not found"
fi

if [ -x "$ROOT/scripts/tce-capture" ]; then
  pass "project-local tce-capture launcher available"
elif command -v tce-capture >/dev/null 2>&1; then
  pass "tce-capture command available"
elif command -v python3 >/dev/null 2>&1 && python3 -c "import tce_cli_capture.cli" >/dev/null 2>&1; then
  pass "tce_cli_capture module available via python3"
else
  warn "tce-capture not installed (run ./scripts/install.sh or use ./scripts/tce-capture after setup)"
fi

mcp_import_ok="false"
if [ -x "$ROOT/scripts/tce-mcp" ]; then
  pass "project-local tce-mcp launcher available"
  mcp_import_ok="true"
elif command -v python3 >/dev/null 2>&1 && (cd "$ROOT" && PYTHONPATH="shared:services/tce_mcp" python3 -c "import tce_mcp.server" >/dev/null 2>&1); then
  pass "tce_mcp server import check"
  mcp_import_ok="true"
fi

full_services="$(docker compose -f "$FULL_COMPOSE" ps --status running --services 2>/dev/null || true)"
lite_services="$(docker compose -f "$LITE_COMPOSE" ps --status running --services 2>/dev/null || true)"

if [ "$mcp_import_ok" != "true" ]; then
  if printf '%s\n' "$full_services" "$lite_services" | grep -Eq '^tce-mcp$|^tce-mcp-secondary$'; then
    pass "MCP runtime available through Docker services"
  else
    warn "tce_mcp host runtime import failed and no MCP Docker services detected"
  fi
fi

case "$MODE" in
  full)
    if [ -n "$full_services" ]; then
      pass "full stack has running services"
    else
      fail "full stack has no running services"
    fi
    ;;
  lite)
    if [ -n "$lite_services" ]; then
      pass "lite stack has running services"
    else
      fail "lite stack has no running services"
    fi
    ;;
  all)
    if [ -n "$full_services" ]; then
      pass "full stack has running services"
    else
      fail "full stack has no running services"
    fi
    if [ -n "$lite_services" ]; then
      pass "lite stack has running services"
    else
      fail "lite stack has no running services"
    fi
    ;;
  auto)
    if [ -n "$full_services" ] || [ -n "$lite_services" ]; then
      pass "at least one stack has running services"
    else
      fail "no stack services detected (run ./scripts/start.sh or ./scripts/install.sh)"
    fi
    ;;
esac

if [ "$MODE" = "lite" ] && [ -z "$full_services" ]; then
  pass "maintenance scheduler optional for lite-only setup"
else
  if [ -x "$ROOT/scripts/enqueue_maintenance.sh" ]; then
    pass "maintenance enqueue script available"
  else
    warn "maintenance enqueue script missing or not executable (expected scripts/enqueue_maintenance.sh)"
  fi

  if command -v crontab >/dev/null 2>&1; then
    if crontab -l 2>/dev/null | grep -Fq "open-timeline-engine-maintenance"; then
      pass "weekly maintenance cron configured"
    else
      warn "weekly maintenance cron not configured (run ./scripts/install.sh fix)"
    fi
  else
    warn "crontab not available; schedule scripts/enqueue_maintenance.sh manually"
  fi
fi

generated_count=0
for file in \
  "$ROOT/docs/mcp-config/generated/claude_desktop_config.json" \
  "$ROOT/docs/mcp-config/generated/codex_desktop_mcp.json" \
  "$ROOT/docs/mcp-config/generated/cursor_mcp.json"; do
  if [ -f "$file" ]; then
    generated_count=$((generated_count + 1))
  fi
done

if [ "$generated_count" -gt 0 ]; then
  pass "generated MCP config pack(s) found (${generated_count})"
else
  warn "no generated MCP configs found (optional; run ./scripts/configure_mcp_clients.sh --client all)"
fi

operation_mode="timeline_only"
if [ -f "$ROOT/.env" ]; then
  detected_mode="$(awk -F= '$1=="TCE_DEFAULT_OPERATION_MODE" {print $2}' "$ROOT/.env" | tail -n1)"
  if [ -n "$detected_mode" ]; then
    operation_mode="$detected_mode"
  fi
fi

if [ "$operation_mode" = "clone_advisor" ]; then
  workspace_root="${PWD}"
  workspace_agents="${workspace_root}/AGENTS.md"
  workspace_claude="${workspace_root}/CLAUDE.md"
  workspace_policy_active="false"

  if [ -f "$workspace_agents" ] && grep -Fq "tce.takeover_step" "$workspace_agents"; then
    workspace_policy_active="true"
  fi
  if [ -f "$workspace_claude" ] && grep -Fq "tce.takeover_step" "$workspace_claude"; then
    workspace_policy_active="true"
  fi

  if [ "$workspace_policy_active" = "true" ]; then
    pass "workspace takeover policy active (${workspace_root})"
  else
    warn "workspace takeover policy missing (${workspace_root}); run ./scripts/apply_takeover_policy.sh --workspace-root \"${workspace_root}\""
    if [ "$workspace_root" != "$ROOT" ]; then
      repo_agents="${ROOT}/AGENTS.md"
      repo_claude="${ROOT}/CLAUDE.md"
      repo_policy_active="false"
      if [ -f "$repo_agents" ] && grep -Fq "tce.takeover_step" "$repo_agents"; then
        repo_policy_active="true"
      fi
      if [ -f "$repo_claude" ] && grep -Fq "tce.takeover_step" "$repo_claude"; then
        repo_policy_active="true"
      fi
      if [ "$repo_policy_active" = "true" ]; then
        warn "policy exists in repo root (${ROOT}) but not in current workspace (${workspace_root})"
      fi
    fi
  fi

  if command -v codex >/dev/null 2>&1; then
    codex_has_servers="false"
    if codex mcp list 2>/dev/null | awk '{print $1}' | grep -Eq '^tce-executor$|^tce-secondary$|^tce-advisor$'; then
      codex_has_servers="true"
    fi
    if [ "$codex_has_servers" = "true" ] && [ "$workspace_policy_active" != "true" ]; then
      warn "Codex MCP servers are configured, but takeover will not auto-invoke in this workspace until policy is applied"
    fi
  fi

  if [ -x "$ROOT/scripts/enqueue_autonomy_tick.sh" ]; then
    pass "autonomy tick script available"
  else
    warn "autonomy tick script missing or not executable (expected scripts/enqueue_autonomy_tick.sh)"
  fi

  if command -v crontab >/dev/null 2>&1; then
    if crontab -l 2>/dev/null | grep -Fq "${AUTONOMY_TICK_CRON_MARKER}"; then
      pass "autonomy tick cron configured"
    else
      warn "autonomy tick cron not configured (run ./scripts/install.sh fix in clone_advisor mode)"
    fi
  else
    warn "crontab not available; schedule scripts/enqueue_autonomy_tick.sh manually"
  fi
fi

# Ollama embedding model check (full stack only)
if printf '%s' "$full_services" | grep -q "ollama"; then
  embed_model="${TCE_EMBED_MODEL:-mxbai-embed-large}"
  if [ -f "$ROOT/.env" ]; then
    env_model="$(awk -F= '/^TCE_EMBED_MODEL=/ {print $2}' "$ROOT/.env")"
    if [ -n "$env_model" ]; then
      embed_model="$env_model"
    fi
  fi
  ollama_models="$(docker compose -f "$FULL_COMPOSE" exec -T ollama ollama list 2>/dev/null || true)"
  if printf '%s' "$ollama_models" | grep -q "$embed_model"; then
    pass "Ollama embedding model loaded (${embed_model})"
  else
    warn "Ollama embedding model '${embed_model}' not loaded (run: docker exec open-timeline-engine-ollama-1 ollama pull ${embed_model})"
  fi
fi

# Self-heal script check
if [ -x "$ROOT/scripts/self-heal.sh" ]; then
  pass "self-heal script executable"
else
  warn "self-heal script missing or not executable (expected scripts/self-heal.sh)"
fi

# Client hooks check
hooks_found="false"
workspace_root="${PWD}"
claude_settings="${workspace_root}/.claude/settings.json"
if [ -f "$claude_settings" ] && command -v python3 >/dev/null 2>&1; then
  if python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
hooks = data.get('hooks', {})
has_submit = 'UserPromptSubmit' in hooks
has_post = 'PostToolUse' in hooks
sys.exit(0 if has_submit and has_post else 1)
" "$claude_settings" 2>/dev/null; then
    pass "Claude Code hooks installed (UserPromptSubmit + PostToolUse)"
    hooks_found="true"
  else
    warn "Claude Code hooks incomplete (missing UserPromptSubmit or PostToolUse in ${claude_settings})"
  fi
else
  warn "No Claude Code hooks found (${claude_settings} missing; run ./scripts/install.sh fix)"
fi

# Takeover auto-activation check
if [ "$operation_mode" = "clone_advisor" ] && command -v curl >/dev/null 2>&1; then
  if curl -fsS -X POST "${API_URL}/v1/takeover/autonomy/tick" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "X-TCE-Workspace: ${TCE_MCP_WORKSPACE_ID:-personal}" \
    -H "X-TCE-User: ${TCE_MCP_USER_ID:-codex-executor}" \
    -H "X-TCE-Consumer: doctor-autonomy-check" \
    -H "X-TCE-Role: executor" \
    -d '{"session_id":"doctor-autonomy-check","include_open_discovery":true,"max_sessions":5}' >/dev/null 2>&1; then
    pass "autonomy tick endpoint reachable (${API_URL}/v1/takeover/autonomy/tick)"
  else
    warn "autonomy tick endpoint unreachable (${API_URL}/v1/takeover/autonomy/tick)"
  fi

  takeover_response=$(curl -sf -X POST "${API_URL}/v1/takeover/step" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "X-TCE-Workspace: ${TCE_MCP_WORKSPACE_ID:-personal}" \
    -H "X-TCE-User: ${TCE_MCP_USER_ID:-codex-executor}" \
    -H "X-TCE-Consumer: doctor-check" \
    -H "X-TCE-Role: executor" \
    -d '{"session_id":"doctor-check","message":"doctor auto-activation test","activation_mode_default":"takeover"}' 2>/dev/null) || takeover_response=""
  if [ -n "$takeover_response" ] && command -v python3 >/dev/null 2>&1; then
    is_active=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['state']['active'])" "$takeover_response" 2>/dev/null || echo "unknown")
    if [ "$is_active" = "True" ]; then
      pass "takeover auto-activation working (activation_mode_default=takeover)"
      curl -sf -X POST "${API_URL}/v1/takeover/reset" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${TOKEN}" \
        -H "X-TCE-Workspace: ${TCE_MCP_WORKSPACE_ID:-personal}" \
        -H "X-TCE-User: ${TCE_MCP_USER_ID:-codex-executor}" \
        -H "X-TCE-Consumer: doctor-check" \
        -H "X-TCE-Role: executor" \
        -d '{"session_id":"doctor-check"}' >/dev/null 2>&1 || true
    else
      warn "takeover auto-activation not working (is API updated with auto-activate fix?)"
    fi
  elif [ -n "$takeover_response" ]; then
    warn "takeover auto-activation check skipped (python3 needed to parse response)"
  else
    warn "takeover auto-activation check failed (API unreachable)"
  fi
fi

echo
echo "Doctor summary: pass=${PASS_COUNT} warn=${WARN_COUNT} fail=${FAIL_COUNT}"

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
