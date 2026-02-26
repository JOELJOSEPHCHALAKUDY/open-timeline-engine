# Setup Script Improvements Design

**Date:** 2026-02-19
**Target:** External users — production-grade installer
**Approach:** Incremental enhancement (Approach A) — add features as functions within existing install.sh

## Problem Statement

The TCE install flow has several gaps that cause issues for users:

1. **Takeover breaks between sessions** — no hook automation means CLAUDE.md policy is advisory-only
2. **No pre-flight validation** — install fails mid-way if deps/ports are missing
3. **Hardcoded absolute paths in launchers** — break if repo moves
4. **No rollback** — partial failures leave .env in inconsistent state
5. **No install logging** — hard to troubleshoot failures
6. **doctor.sh doesn't check hooks or self-heal** — can't diagnose the #1 issue

## Features

### 1. Pre-flight Checks

New `preflight_checks()` function called before any modifications.

**Hard checks (exit on failure):**
- Docker installed and daemon running
- Docker Compose available
- Python 3.11+ available
- curl available

**Soft checks (warn, ask to continue):**
- Port availability (8080, 5432, 6379, 9090, 3000) via `lsof -i`
- Disk space >= 2GB free
- Network connectivity (Docker Hub reachable)

`--yes` mode: soft failures auto-continue with warning.

### 2. .env Backup & Rollback

Before any `set_env_key()` calls:
```bash
cp .env .env.backup.$(date +%s)
```

On failure via `trap ERR/EXIT`: restore from backup.
On success: remove current backup, rotate to keep last 3 backups only.

### 3. Runtime-Resolved Launcher Paths

Replace hardcoded absolute paths in generated launchers with runtime resolution.

**tce-mcp launcher:**
```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"${ROOT}/.venv_mcp/bin/python" -m tce_mcp.server "$@"
```

**tce-capture launcher:**
```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then
  set -a; . "$ROOT/.env"; set +a
fi
"${ROOT}/.venv_cli_capture/bin/tce-capture" "$@"
```

External MCP configs (Claude Desktop, Cursor) stay absolute (required by those tools) but include a comment noting how to update paths if the repo moves.

### 4. Hook Setup Automation

New `install_client_hooks()` function — Step 12 after takeover policy application.

**Claude Code** — generates/merges `.claude/settings.json` in workspace root:
- `UserPromptSubmit` hook: system message to call `tce_takeover_step` every turn
- `PostToolUse` hook on `Edit|Write`: system message to run `self-heal.sh` + re-activate takeover when TCE files modified

**Cursor** — generates `.cursor/settings.json` equivalent if hooks are supported, otherwise skip with note.

**Codex** — generates equivalent config if hooks are supported.

**Behavior:**
- Detects installed clients by checking config directory existence
- Merges into existing settings (preserves user's other settings) via inline Python JSON manipulation
- Interactive: asks "Install auto-takeover hooks for [clients]? [Y/n]"
- `--yes` mode: installs for all detected clients

### 5. Self-Heal Integration

Wire existing `scripts/self-heal.sh` into the install flow:

**install.sh `fix` action:** Enhanced to run `self-heal.sh` (rebuild + re-activate takeover) in addition to doctor + start.

**doctor.sh new checks:**
- `[PASS/FAIL] Self-heal script executable`
- `[PASS/FAIL] Client hooks installed` — checks `.claude/settings.json` has required hooks
- `[PASS/FAIL] Takeover auto-activation` — verifies the `activation_mode_default=takeover` code path works

**Install summary** prints self-heal and hook info.

### 6. Unattended Mode Enhancement

Enhance existing `--yes` / `-y` flag so ALL optional steps use sensible defaults:
- Import git history: yes (use repo root)
- Seed patterns: yes
- Configure MCP clients: yes (all detected)
- Install hooks: yes (all detected)
- Run self-test: yes

Enables CI/CD: `./scripts/install.sh install full --behavior clone_advisor --yes`

### 7. Install Logging

All output tee'd to `$ROOT/install.log`:
```bash
exec > >(tee "$ROOT/install.log") 2>&1
```

Added at top of script. On failure, error message references the log file for troubleshooting.

## Files Modified

| File | Changes |
|------|---------|
| `scripts/install.sh` | +7 features as functions (~300 lines) |
| `scripts/doctor.sh` | +3 new checks (~40 lines) |
| `scripts/configure_mcp_clients.sh` | +path comments (~5 lines) |

## Files Unchanged

All other scripts (start.sh, stop.sh, assign_ai_roles.sh, apply_takeover_policy.sh, self-heal.sh, set_mode.sh, chat_with_advisor.sh) remain untouched.

## Decisions

- **Single file**: Keep install.sh as one file (no modularization)
- **Path strategy**: Runtime resolution — always resolves from script location
- **Rollback scope**: .env backup only (simple, covers most common issue)
- **Hook targets**: All supported clients (Claude Code, Cursor, Codex)
