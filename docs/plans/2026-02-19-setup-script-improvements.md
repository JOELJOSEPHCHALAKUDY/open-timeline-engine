# Setup Script Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Harden the TCE install flow for external users — add pre-flight checks, .env rollback, runtime launcher paths, hook automation, self-heal integration, unattended mode, and install logging.

**Architecture:** All changes go into existing files (install.sh, doctor.sh, configure_mcp_clients.sh). New functions are added inline. No new files created (self-heal.sh already exists).

**Tech Stack:** Bash, inline Python for JSON merging, Docker Compose, curl

---

### Task 1: Install Logging

**Files:**
- Modify: `scripts/install.sh:1-10`

**Step 1: Add logging setup after variable declarations**

Insert after line 9 (after `LOCAL_MCP_LAUNCHER` declaration):

```bash
INSTALL_LOG="${ROOT}/install.log"
exec > >(tee "$INSTALL_LOG") 2>&1
echo "Install log: ${INSTALL_LOG}"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo
```

**Step 2: Verify logging works**

Run: `./scripts/install.sh --help 2>&1 | head -5`
Expected: output appears on screen AND in install.log

**Step 3: Commit**

```bash
git add scripts/install.sh
git commit -m "feat(install): add install logging to install.log"
```

---

### Task 2: .env Backup & Rollback

**Files:**
- Modify: `scripts/install.sh:116-128` (after `set_env_key` definition)

**Step 1: Add backup and rollback functions**

Insert after the `set_env_key()` function (after line 128):

```bash
backup_env() {
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
```

**Step 2: Add trap for rollback**

Insert just before the `if [ "$ACTION" != "doctor" ]` block (before line 514):

```bash
install_failed() {
  echo
  echo "Install failed. Check log: ${INSTALL_LOG:-install.log}"
  restore_env
  exit 1
}
trap install_failed ERR
```

**Step 3: Call backup_env before .env modifications**

Insert just before the first `set_env_key` call (before line 1033):

```bash
backup_env
```

**Step 4: Call cleanup on success**

Insert at the very end of install (before the final summary around line 1132):

```bash
cleanup_env_backups
trap - ERR
```

**Step 5: Test rollback by simulating failure**

Run: `./scripts/install.sh install full --yes` (should succeed, backup cleaned up)
Check: `ls .env.backup.*` should show at most 3 backups

**Step 6: Commit**

```bash
git add scripts/install.sh
git commit -m "feat(install): add .env backup and rollback on failure"
```

---

### Task 3: Pre-flight Checks

**Files:**
- Modify: `scripts/install.sh` (insert new function before the existing docker check at line 514)

**Step 1: Add preflight_checks function**

Insert before line 514 (`if [ "$ACTION" != "doctor" ]`):

```bash
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
```

**Step 2: Replace existing docker check with preflight call**

Replace lines 514-524 (the existing docker/compose checks):

```bash
if [ "$ACTION" = "install" ] || [ "$ACTION" = "fix" ] || [ "$ACTION" = "restart" ]; then
  preflight_checks
fi
```

**Step 3: Test preflight**

Run: `./scripts/install.sh doctor auto` (should skip preflight)
Run: `./scripts/install.sh install full --yes` (should run preflight, then install)

**Step 4: Commit**

```bash
git add scripts/install.sh
git commit -m "feat(install): add pre-flight checks for deps, ports, disk space"
```

---

### Task 4: Runtime-Resolved Launcher Paths

**Files:**
- Modify: `scripts/install.sh:229-268` (the launcher generation in `install_cli_capture_runtime` and `install_mcp_runtime`)

**Step 1: Fix tce-capture launcher template**

Replace lines 229-240 in `install_cli_capture_runtime()`:

```bash
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
"${ROOT}/.venv_cli_capture/bin/tce-capture" "$@"
LAUNCHER
```

Note: use `<<'LAUNCHER'` (single-quoted heredoc) so variables are NOT expanded at generation time — they resolve at runtime.

**Step 2: Fix tce-mcp launcher template**

Replace lines 262-266 in `install_mcp_runtime()`:

```bash
  cat > "$LOCAL_MCP_LAUNCHER" <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="${ROOT}/shared:${ROOT}/services/tce_mcp"
export PYTHONPATH
"${ROOT}/.venv_mcp/bin/python" -m tce_mcp.server "$@"
LAUNCHER
```

**Step 3: Test launchers work with relative paths**

Run: `./scripts/tce-mcp --help` (should work)
Run: `./scripts/tce-capture --help` (should work)

**Step 4: Commit**

```bash
git add scripts/install.sh
git commit -m "fix(install): use runtime-resolved paths in launchers"
```

---

### Task 5: Hook Setup Automation

**Files:**
- Modify: `scripts/install.sh` (add new function + Step 12 integration)

**Step 1: Add install_client_hooks function**

Insert after `ensure_workspace_takeover_policy()` (after line 471):

```bash
install_client_hooks() {
  local workspace_root="$1"
  local self_heal_path="${ROOT}/scripts/self-heal.sh"

  echo "Step 12 - Client hooks"

  # Build the hooks JSON
  local hooks_json
  hooks_json=$(python3 -c "
import json, sys, os

self_heal = sys.argv[1]
hooks = {
    'hooks': {
        'UserPromptSubmit': [{
            'matcher': '*',
            'hooks': [{
                'type': 'command',
                'command': 'echo \'{\\"systemMessage\\": \\"[TCE-HOOK] You MUST call mcp__tce-advisor__tce_takeover_step BEFORE responding. Use session_id=\\\\\\\"default\\\\\\\", message=the user prompt, activation_mode_default=\\\\\\\"takeover\\\\\\\". If final_response is returned, use it as your response. If safety_decision=confirm_required, ask the user for confirmation before proceeding.\\"}\'',
                'timeout': 10
            }]
        }],
        'PostToolUse': [{
            'matcher': 'Edit|Write',
            'hooks': [{
                'type': 'command',
                'command': f'if echo \"\$TOOL_INPUT\" | grep -q \"open-timeline-engine\"; then echo \'{{\"systemMessage\": \"[TCE-SELF-HEAL] You just modified a TCE file. You MUST now: (1) Run: bash {self_heal} (2) Call mcp__tce-advisor__tce_takeover_step with session_id=default, message=self-heal complete, activation_mode_default=takeover.\"}}\'; else echo \'{{\"continue\": true}}\'; fi',
                'timeout': 10
            }]
        }]
    }
}
print(json.dumps(hooks, indent=2))
" "$self_heal_path")

  # Detect and install for each client
  local installed_any="false"

  # Claude Code
  local claude_settings_dir="${workspace_root}/.claude"
  local claude_settings="${claude_settings_dir}/settings.json"
  if [ -d "$claude_settings_dir" ] || [ -d "${HOME}/.claude" ]; then
    mkdir -p "$claude_settings_dir"
    if [ -f "$claude_settings" ]; then
      # Merge with existing
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
    echo "  [NOTE] Codex hook support: check Codex docs for hook configuration format."
    echo "  Generated hook JSON saved to: ${ROOT}/docs/mcp-config/generated/client_hooks.json"
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
```

**Step 2: Wire into install flow**

Insert after line 1083 (after `ensure_workspace_takeover_policy`):

```bash
if [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
  read -r -p "Install auto-takeover client hooks? [Y/n]: " hooks_choice
  case "${hooks_choice:-Y}" in
    n|N|no|NO) ;;
    *) install_client_hooks "${PWD}" ;;
  esac
elif [ "$ASSUME_YES" = "true" ]; then
  install_client_hooks "${PWD}"
fi
```

**Step 3: Test hook installation**

Run: `./scripts/install.sh install full --behavior clone_advisor --yes`
Check: `.claude/settings.json` should contain `UserPromptSubmit` and `PostToolUse` hooks

**Step 4: Commit**

```bash
git add scripts/install.sh
git commit -m "feat(install): auto-install client hooks for takeover + self-heal"
```

---

### Task 6: Self-Heal Integration into Fix Action

**Files:**
- Modify: `scripts/install.sh:614-673` (the `fix` action block)

**Step 1: Add self-heal call to fix action**

Insert after line 653 (after `install_mcp_runtime || true`) in the fix block:

```bash
  if [ -x "${ROOT}/scripts/self-heal.sh" ]; then
    echo "Running self-heal (rebuild + re-activate takeover)..."
    "${ROOT}/scripts/self-heal.sh" --service tce-api || true
  fi
```

**Step 2: Also install hooks during fix**

Insert after the `configure_mcp_clients.sh` call in the fix block (after line 664):

```bash
  install_client_hooks "${PWD}" || true
```

**Step 3: Test fix action**

Run: `./scripts/install.sh fix auto`
Expected: services rebuild, self-heal runs, hooks installed, doctor runs

**Step 4: Commit**

```bash
git add scripts/install.sh
git commit -m "feat(install): integrate self-heal and hook setup into fix action"
```

---

### Task 7: Doctor Checks for Hooks and Self-Heal

**Files:**
- Modify: `scripts/doctor.sh:240-248` (before summary)

**Step 1: Add self-heal check**

Insert before line 242 (before the summary `echo`):

```bash
if [ -x "$ROOT/scripts/self-heal.sh" ]; then
  pass "self-heal script executable"
else
  warn "self-heal script missing or not executable (expected scripts/self-heal.sh)"
fi
```

**Step 2: Add client hooks check**

Insert after the self-heal check:

```bash
hooks_found="false"
workspace_root="${PWD}"
claude_settings="${workspace_root}/.claude/settings.json"
if [ -f "$claude_settings" ]; then
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
```

**Step 3: Add takeover auto-activation check**

Insert after the hooks check:

```bash
if [ "$operation_mode" = "clone_advisor" ] && command -v curl >/dev/null 2>&1; then
  takeover_response=$(curl -sf -X POST "${API_URL}/v1/takeover/step" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "X-TCE-Workspace: ${TCE_MCP_WORKSPACE_ID:-personal}" \
    -H "X-TCE-User: ${TCE_MCP_USER_ID:-codex-executor}" \
    -H "X-TCE-Consumer: doctor-check" \
    -H "X-TCE-Role: executor" \
    -d '{"session_id":"doctor-check","message":"doctor auto-activation test","activation_mode_default":"takeover"}' 2>/dev/null) || takeover_response=""
  if [ -n "$takeover_response" ]; then
    is_active=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['state']['active'])" "$takeover_response" 2>/dev/null || echo "unknown")
    if [ "$is_active" = "True" ]; then
      pass "takeover auto-activation working (activation_mode_default=takeover)"
      # Clean up test session
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
  else
    warn "takeover auto-activation check failed (API unreachable)"
  fi
fi
```

**Step 4: Test doctor checks**

Run: `./scripts/doctor.sh auto`
Expected: new PASS/WARN lines for self-heal, hooks, and auto-activation

**Step 5: Commit**

```bash
git add scripts/doctor.sh
git commit -m "feat(doctor): add checks for self-heal, hooks, and takeover auto-activation"
```

---

### Task 8: Unattended Mode Enhancement

**Files:**
- Modify: `scripts/install.sh` (multiple locations where `ASSUME_YES` is checked)

**Step 1: Make bootstrap_timeline_data work in --yes mode**

Replace lines 312-315 in `bootstrap_timeline_data()`:

```bash
  if [ ! -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
    return
  fi
```

And replace the interactive prompt (lines 322-341) to auto-proceed when `ASSUME_YES`:

```bash
  local import_choice="Y"
  if [ "$ASSUME_YES" != "true" ] && [ -t 0 ]; then
    echo "Bootstrap timeline data (optional)"
    read -r -p "Import existing git history now? [Y/n]: " import_choice
  fi
  case "${import_choice:-Y}" in
```

**Step 2: Make configure_mcp_clients_prompt work in --yes mode**

Replace lines 365-367 in `configure_mcp_clients_prompt()`:

```bash
  if [ ! -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
    return
  fi

  local mcp_choice="Y"
  if [ "$ASSUME_YES" != "true" ] && [ -t 0 ]; then
    echo "Configure MCP clients (optional)"
    read -r -p "Write MCP config to Claude/Codex/Cursor now? [Y/n]: " mcp_choice
  fi
```

**Step 3: Make apply_takeover_policy_prompt work in --yes mode**

Similar pattern — auto-proceed when `ASSUME_YES=true`.

**Step 4: Make self-test run in --yes mode**

The existing self-test block (line 1020-1031) already defaults to N. For `--yes` mode, change default to Y:

```bash
if [ "$OPERATION_MODE" = "clone_advisor" ]; then
  if [ "$ASSUME_YES" = "true" ]; then
    RUN_TAKEOVER_SELF_TEST="true"
  elif [ -t 0 ]; then
    ...existing interactive prompt...
  fi
fi
```

**Step 5: Test unattended install**

Run: `echo "" | ./scripts/install.sh install full --behavior clone_advisor --yes`
Expected: full install completes with zero prompts, all optional steps use defaults

**Step 6: Commit**

```bash
git add scripts/install.sh
git commit -m "feat(install): enhance --yes mode for fully unattended installs"
```

---

### Task 9: Configure MCP Clients Path Comments

**Files:**
- Modify: `scripts/configure_mcp_clients.sh` (in the JSON generation sections)

**Step 1: Add path comment to generated configs**

Find where the Claude Desktop config JSON is generated and add a note. The generated configs are JSON so we can't add comments directly, but we can print a note to stdout:

```bash
echo "NOTE: Generated MCP configs use absolute paths. If you move the repo, re-run:"
echo "  ./scripts/configure_mcp_clients.sh --client all"
```

Add this after each config file is written.

**Step 2: Commit**

```bash
git add scripts/configure_mcp_clients.sh
git commit -m "docs(mcp-config): add path update note to generated configs"
```

---

### Task 10: Update Install Summary

**Files:**
- Modify: `scripts/install.sh:1132-1164` (the final summary)

**Step 1: Add self-heal and hooks info to summary**

Insert after the existing summary items:

```bash
echo "Self-heal: ${ROOT}/scripts/self-heal.sh"
if [ -f "${PWD}/.claude/settings.json" ]; then
  echo "Client hooks: ${PWD}/.claude/settings.json (UserPromptSubmit + PostToolUse)"
fi
echo "Install log: ${INSTALL_LOG}"
```

**Step 2: Test full install end-to-end**

Run: `./scripts/install.sh install full --behavior clone_advisor --yes`
Verify: summary includes all new items

**Step 3: Commit**

```bash
git add scripts/install.sh
git commit -m "feat(install): update summary with self-heal, hooks, and log info"
```

---

### Task 11: Final Integration Test

**Step 1: Run full unattended install**

```bash
./scripts/install.sh install full --behavior clone_advisor --yes
```

Expected: all steps complete, hooks installed, self-heal available

**Step 2: Run doctor**

```bash
./scripts/doctor.sh auto
```

Expected: all PASS, including new self-heal/hooks/auto-activation checks

**Step 3: Run fix**

```bash
./scripts/install.sh fix auto
```

Expected: rebuilds services, runs self-heal, installs hooks, doctor passes

**Step 4: Final commit**

```bash
git add -A
git commit -m "feat(install): setup script improvements - preflight, rollback, hooks, self-heal, unattended"
```
