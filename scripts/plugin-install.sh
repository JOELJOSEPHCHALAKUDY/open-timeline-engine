#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"

# ── Defaults ─────────────────────────────────────────────────────────────────
SELECT_VSCODE="false"
SELECT_BROWSER="false"
SELECT_GIT="false"
BUILD_FROM_SOURCE="false"
ASSUME_YES="false"
GIT_REPO_PATH=""

# ── Colors ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
err()     { echo -e "${RED}✗${NC} $*" >&2; }
header()  { echo -e "\n${BOLD}${CYAN}── $* ──${NC}\n"; }

# ── Usage ────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
${BOLD}Open Timeline Engine — Plugin Installer${NC}

Usage: ./scripts/plugin-install.sh [options]

${BOLD}Plugin selectors:${NC}
  --vscode       Install VSCode extension
  --browser      Install browser extension (Chrome/Edge)
  --git          Install git capture hooks
  --all          Install all plugins

${BOLD}Options:${NC}
  --build        Build from source (default: use pre-built artifacts)
  --repo <path>  Target repo for git hooks (default: prompts or current dir)
  -y, --yes      Non-interactive mode (skip prompts)
  -h, --help     Show this help

${BOLD}Examples:${NC}
  ./scripts/plugin-install.sh                    # Interactive menu
  ./scripts/plugin-install.sh --all              # Install all plugins
  ./scripts/plugin-install.sh --vscode           # VSCode only
  ./scripts/plugin-install.sh --git --repo .     # Git hooks for current repo
  ./scripts/plugin-install.sh --all --build      # Build everything from source
  ./scripts/plugin-install.sh --all -y           # Non-interactive, all plugins
EOF
}

# ── Parse args ───────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --vscode)  SELECT_VSCODE="true" ;;
    --browser) SELECT_BROWSER="true" ;;
    --git)     SELECT_GIT="true" ;;
    --all)
      SELECT_VSCODE="true"
      SELECT_BROWSER="true"
      SELECT_GIT="true"
      ;;
    --build)   BUILD_FROM_SOURCE="true" ;;
    --repo)
      shift
      GIT_REPO_PATH="${1:?'--repo requires a path'}"
      ;;
    -y|--yes)  ASSUME_YES="true" ;;
    -h|--help) usage; exit 0 ;;
    *)
      err "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
  shift
done

# ── Source .env if available ─────────────────────────────────────────────────
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# ── Interactive menu (when no flags given) ───────────────────────────────────
show_menu() {
  header "Plugin Installer"
  echo "Available capture plugins:"
  echo ""
  echo -e "  ${BOLD}1)${NC} VSCode Extension     — capture task lifecycle, decisions, document saves"
  echo -e "  ${BOLD}2)${NC} Browser Extension    — capture web activity per allowed site (Chrome/Edge)"
  echo -e "  ${BOLD}3)${NC} Git Hooks            — capture commits and pushes automatically"
  echo ""
  echo -e "  ${BOLD}a)${NC} All of the above"
  echo -e "  ${BOLD}q)${NC} Quit"
  echo ""
  read -r -p "Select plugins to install (comma-separated, e.g. 1,3): " choice </dev/tty || choice="q"

  for c in $(echo "$choice" | tr ',' ' '); do
    case "$c" in
      1) SELECT_VSCODE="true" ;;
      2) SELECT_BROWSER="true" ;;
      3) SELECT_GIT="true" ;;
      a|A) SELECT_VSCODE="true"; SELECT_BROWSER="true"; SELECT_GIT="true" ;;
      q|Q) echo "Cancelled."; exit 0 ;;
      *)   warn "Unknown selection: $c (skipped)" ;;
    esac
  done

  if [ "$SELECT_VSCODE" = "false" ] && [ "$SELECT_BROWSER" = "false" ] && [ "$SELECT_GIT" = "false" ]; then
    err "No plugins selected."
    exit 1
  fi
}

if [ "$SELECT_VSCODE" = "false" ] && [ "$SELECT_BROWSER" = "false" ] && [ "$SELECT_GIT" = "false" ]; then
  show_menu
fi

# ── Helpers ──────────────────────────────────────────────────────────────────
require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    err "Required command not found: $1"
    return 1
  fi
}

ensure_node_deps() {
  require_cmd npm
  if [ ! -d "${ROOT}/node_modules" ]; then
    echo "Installing workspace Node dependencies..."
    (cd "$ROOT" && npm install)
  fi
}

ensure_python3() {
  if ! command -v python3 >/dev/null 2>&1; then
    err "python3 is required but not found."
    return 1
  fi
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
    err "python3 >= 3.12 is required (you have $(python3 --version 2>&1))."
    return 1
  fi
}

# ── VSCode Extension ────────────────────────────────────────────────────────
install_vscode() {
  header "VSCode Extension"

  local vsix_path=""

  if [ "$BUILD_FROM_SOURCE" = "true" ]; then
    # Check Node version for vsce (requires >= 20)
    local node_major
    node_major="$(node -v 2>/dev/null | sed 's/v\([0-9]*\).*/\1/' || echo "0")"
    if [ "$node_major" -lt 20 ]; then
      err "VSCode VSIX packaging requires Node.js >= 20 (you have $(node -v 2>/dev/null || echo 'none'))."
      echo "  Upgrade Node: nvm install 20 && nvm use 20"
      echo "  Then re-run: ./scripts/plugin-install.sh --vscode --build"
      return 1
    fi

    echo "Building from source..."
    ensure_node_deps
    (cd "$ROOT" && npm run -w plugins/tce_vscode build)

    if ! (cd "$ROOT" && npx -w plugins/tce_vscode vsce --version >/dev/null 2>&1); then
      echo "Installing vsce..."
      (cd "$ROOT" && npm i -w plugins/tce_vscode -D @vscode/vsce)
    fi

    echo "Packaging VSIX..."
    (cd "${ROOT}/plugins/tce_vscode" && npx vsce package --allow-missing-repository --no-dependencies)
    vsix_path="$(ls -1t "${ROOT}/plugins/tce_vscode"/*.vsix 2>/dev/null | head -n 1 || true)"
  else
    # Use pre-built .vsix
    vsix_path="$(ls -1t "${ROOT}/plugins/tce_vscode"/*.vsix 2>/dev/null | head -n 1 || true)"
    if [ -z "$vsix_path" ]; then
      warn "No pre-built .vsix found. Attempting build from source..."
      # Build without modifying global flag
      local node_major
      node_major="$(node -v 2>/dev/null | sed 's/v\([0-9]*\).*/\1/' || echo "0")"
      if [ "$node_major" -lt 20 ]; then
        err "VSCode VSIX packaging requires Node.js >= 20 (you have $(node -v 2>/dev/null || echo 'none'))."
        echo "  Upgrade Node: nvm install 20 && nvm use 20"
        echo "  Then re-run: ./scripts/plugin-install.sh --vscode --build"
        return 1
      fi
      ensure_node_deps
      (cd "$ROOT" && npm run -w plugins/tce_vscode build)
      if ! (cd "$ROOT" && npx -w plugins/tce_vscode vsce --version >/dev/null 2>&1); then
        (cd "$ROOT" && npm i -w plugins/tce_vscode -D @vscode/vsce)
      fi
      (cd "${ROOT}/plugins/tce_vscode" && npx vsce package --allow-missing-repository --no-dependencies)
      vsix_path="$(ls -1t "${ROOT}/plugins/tce_vscode"/*.vsix 2>/dev/null | head -n 1 || true)"
    fi
  fi

  if [ -z "$vsix_path" ]; then
    err "Failed to find or build .vsix file."
    return 1
  fi

  info "VSIX ready: ${vsix_path}"

  # Try auto-install
  if command -v code >/dev/null 2>&1; then
    echo "Installing extension into VS Code..."
    if code --install-extension "$vsix_path" 2>/dev/null; then
      info "VSCode extension installed successfully!"
    else
      warn "Auto-install failed. Install manually (see below)."
    fi
  else
    warn "'code' CLI not found. Install manually:"
    echo ""
    echo "  code --install-extension ${vsix_path}"
    echo ""
    echo "  Or in VS Code: Extensions → ⋯ → Install from VSIX → select the file above"
  fi

  echo ""
  echo -e "${BOLD}Configure in VS Code Settings:${NC}"
  echo "  tce.apiUrl       → ${TCE_API_BASE_URL:-http://localhost:8080}"
  echo "  tce.apiToken     → (your TCE API token)"
  echo "  tce.workspaceId  → ${TCE_MCP_WORKSPACE_ID:-personal}"
  echo ""
}

# ── Browser Extension ───────────────────────────────────────────────────────
install_browser() {
  header "Browser Extension"

  local dist_dir="${ROOT}/plugins/tce_browser/dist"

  if [ "$BUILD_FROM_SOURCE" = "true" ]; then
    echo "Building from source..."
    ensure_node_deps
    (cd "$ROOT" && npm run -w plugins/tce_browser build)
  else
    if [ ! -d "$dist_dir" ] || [ ! -f "${dist_dir}/manifest.json" ]; then
      warn "No pre-built extension found. Building from source..."
      ensure_node_deps
      (cd "$ROOT" && npm run -w plugins/tce_browser build)
    fi
  fi

  if [ ! -d "$dist_dir" ]; then
    err "Failed to build browser extension."
    return 1
  fi

  info "Browser extension ready: ${dist_dir}"
  echo ""
  echo -e "${BOLD}To load in Chrome / Edge:${NC}"
  echo "  1. Open chrome://extensions (or edge://extensions)"
  echo "  2. Enable \"Developer mode\" (top-right toggle)"
  echo "  3. Click \"Load unpacked\""
  echo "  4. Select: ${dist_dir}"
  echo ""
  echo -e "${BOLD}Configure in extension options:${NC}"
  echo "  API URL   → ${TCE_API_BASE_URL:-http://localhost:8080}"
  echo "  API Token → (your TCE API token)"
  echo "  Add allowed site hostnames for capture"
  echo ""
}

# ── Git Hooks ────────────────────────────────────────────────────────────────
install_git() {
  header "Git Capture Hooks"

  ensure_python3 || return 1

  echo "Installing tce-git-capture package..."
  if ! python3 -m pip install -e "${ROOT}/plugins/tce_git_capture" 2>&1; then
    err "Failed to install tce-git-capture. Check Python version (>= 3.12 required)."
    return 1
  fi

  # Find the command — may be in a non-PATH location
  local git_capture_cmd=""
  if command -v tce-git-capture >/dev/null 2>&1; then
    git_capture_cmd="tce-git-capture"
  elif python3 -c "from tce_git_capture.cli import app" >/dev/null 2>&1; then
    git_capture_cmd="python3 -m tce_git_capture.cli"
  else
    err "tce-git-capture not available after install."
    echo "Try: python3 -m pip install -e ${ROOT}/plugins/tce_git_capture"
    return 1
  fi

  info "tce-git-capture installed."

  # Determine target repo
  local repo_path="$GIT_REPO_PATH"

  if [ -z "$repo_path" ]; then
    if [ "$ASSUME_YES" = "true" ]; then
      repo_path="$PWD"
    else
      read -r -p "Which repo should I install git hooks into? [${PWD}]: " repo_path </dev/tty || repo_path=""
      repo_path="${repo_path:-$PWD}"
    fi
  fi

  # Validate it's a git repo
  if [ ! -d "${repo_path}/.git" ]; then
    err "Not a git repository: ${repo_path}"
    echo "Run this inside a git repo or pass --repo <path>"
    return 1
  fi

  echo "Installing git hooks into: ${repo_path}"
  $git_capture_cmd install --repo "$repo_path"

  info "Git hooks installed!"
  echo ""
  echo -e "${BOLD}Hooks installed:${NC}"
  echo "  post-commit  — captures every commit"
  echo "  pre-push     — captures push events"
  echo ""
  echo -e "${BOLD}Required env vars (set in your shell or .env):${NC}"
  echo "  TCE_API_BASE_URL  → ${TCE_API_BASE_URL:-http://localhost:8080}"
  echo "  TCE_API_TOKEN     → (your TCE API token)"
  echo "  TCE_MCP_WORKSPACE_ID → ${TCE_MCP_WORKSPACE_ID:-personal}"
  echo ""
}

# ── Run selected installs ────────────────────────────────────────────────────
header "Open Timeline Engine — Plugin Setup"
echo -e "Mode: ${BOLD}$([ "$BUILD_FROM_SOURCE" = "true" ] && echo "build from source" || echo "pre-built artifacts")${NC}"
echo ""

INSTALLED=0

if [ "$SELECT_VSCODE" = "true" ]; then
  install_vscode && INSTALLED=$((INSTALLED + 1)) || warn "VSCode install had issues (see above)"
fi

if [ "$SELECT_BROWSER" = "true" ]; then
  install_browser && INSTALLED=$((INSTALLED + 1)) || warn "Browser install had issues (see above)"
fi

if [ "$SELECT_GIT" = "true" ]; then
  install_git && INSTALLED=$((INSTALLED + 1)) || warn "Git install had issues (see above)"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
header "Done"
if [ "$INSTALLED" -gt 0 ]; then
  info "${INSTALLED} plugin(s) installed successfully."
else
  warn "No plugins were installed."
fi
echo ""
echo "Docs: docs/plugin-setup.md"
echo "Troubleshoot: ./scripts/doctor.sh auto"
echo ""
