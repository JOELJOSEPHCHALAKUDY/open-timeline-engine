#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ACTION="install"
ASSUME_YES="false"
SELECT_CLI="false"
SELECT_GIT="false"
SELECT_VSCODE="false"
SELECT_BROWSER="false"

usage() {
  cat <<'EOF'
Usage: ./scripts/setup_plugins.sh [install|fix|repair|uninstall] [options]

Actions:
  install      Install/build selected plugins (default)
  fix           Repair selected plugins (reinstall/rebuild)
  repair        Alias for fix
  uninstall     Remove selected plugin artifacts/installations

Plugin selectors:
  --all         Select all plugins (default if none selected)
  --cli         CLI capture plugin
  --git         Git capture plugin
  --vscode      VS Code plugin
  --browser     Browser plugin

Other options:
  -y, --yes     Non-interactive mode
  -h, --help    Show this help

Examples:
  ./scripts/setup_plugins.sh install --all
  ./scripts/setup_plugins.sh fix --cli --git
  ./scripts/setup_plugins.sh repair --vscode --browser
  ./scripts/setup_plugins.sh uninstall --all --yes
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    install|setup)
      ACTION="install"
      ;;
    fix|repair|heal)
      ACTION="fix"
      ;;
    uninstall|remove|purge)
      ACTION="uninstall"
      ;;
    --all)
      SELECT_CLI="true"
      SELECT_GIT="true"
      SELECT_VSCODE="true"
      SELECT_BROWSER="true"
      ;;
    --cli)
      SELECT_CLI="true"
      ;;
    --git)
      SELECT_GIT="true"
      ;;
    --vscode)
      SELECT_VSCODE="true"
      ;;
    --browser)
      SELECT_BROWSER="true"
      ;;
    -y|--yes)
      ASSUME_YES="true"
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

if [ "$SELECT_CLI" = "false" ] && [ "$SELECT_GIT" = "false" ] && [ "$SELECT_VSCODE" = "false" ] && [ "$SELECT_BROWSER" = "false" ]; then
  SELECT_CLI="true"
  SELECT_GIT="true"
  SELECT_VSCODE="true"
  SELECT_BROWSER="true"
fi

confirm_uninstall_if_needed() {
  if [ "$ACTION" != "uninstall" ] || [ "$ASSUME_YES" = "true" ]; then
    return
  fi
  local reply
  read -r -p "This will uninstall selected plugins/artifacts. Continue? [y/N]: " reply </dev/tty || reply="n"
  case "${reply}" in
    y|Y|yes|YES)
      ;;
    *)
      echo "Cancelled."
      exit 0
      ;;
  esac
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
}

ensure_node_deps() {
  require_cmd npm
  if [ ! -d "${ROOT}/node_modules" ]; then
    echo "Installing workspace Node dependencies..."
    (cd "$ROOT" && npm install)
  fi
}

install_cli() {
  require_cmd python3
  echo "[cli] Installing tce_cli_capture..."
  python3 -m pip install -e "${ROOT}/plugins/tce_cli_capture"
}

fix_cli() {
  require_cmd python3
  echo "[cli] Repairing tce_cli_capture..."
  python3 -m pip install --upgrade --force-reinstall -e "${ROOT}/plugins/tce_cli_capture"
}

uninstall_cli() {
  require_cmd python3
  echo "[cli] Uninstalling tce_cli_capture..."
  python3 -m pip uninstall -y tce-cli-capture tce_cli_capture >/dev/null 2>&1 || true
}

install_git() {
  require_cmd python3
  echo "[git] Installing tce_git_capture..."
  python3 -m pip install -e "${ROOT}/plugins/tce_git_capture"
}

fix_git() {
  require_cmd python3
  echo "[git] Repairing tce_git_capture..."
  python3 -m pip install --upgrade --force-reinstall -e "${ROOT}/plugins/tce_git_capture"
}

uninstall_git() {
  require_cmd python3
  echo "[git] Uninstalling tce_git_capture..."
  python3 -m pip uninstall -y tce-git-capture tce_git_capture >/dev/null 2>&1 || true
}

package_vscode() {
  ensure_node_deps
  echo "[vscode] Building extension..."
  (cd "$ROOT" && npm run -w plugins/tce_vscode build)
  if ! (cd "$ROOT" && npx -w plugins/tce_vscode vsce --version >/dev/null 2>&1); then
    echo "[vscode] Installing vsce..."
    (cd "$ROOT" && npm i -w plugins/tce_vscode -D @vscode/vsce)
  fi
  echo "[vscode] Packaging VSIX..."
  (cd "$ROOT" && npx -w plugins/tce_vscode vsce package)
  local vsix_path
  vsix_path="$(ls -1t "${ROOT}/plugins/tce_vscode"/*.vsix 2>/dev/null | head -n 1 || true)"
  if [ -n "$vsix_path" ]; then
    echo "[vscode] VSIX ready: ${vsix_path}"
  fi
}

install_vscode() {
  package_vscode
}

fix_vscode() {
  echo "[vscode] Repairing extension build artifacts..."
  rm -rf "${ROOT}/plugins/tce_vscode/dist"
  package_vscode
}

uninstall_vscode() {
  echo "[vscode] Removing local build/package artifacts..."
  rm -rf "${ROOT}/plugins/tce_vscode/dist"
  rm -f "${ROOT}/plugins/tce_vscode"/*.vsix
  echo "[vscode] If installed in VS Code, remove it from Extensions UI."
}

build_browser() {
  ensure_node_deps
  echo "[browser] Building extension..."
  (cd "$ROOT" && npm run -w plugins/tce_browser build)
  echo "[browser] Unpacked extension path: ${ROOT}/plugins/tce_browser/dist"
}

install_browser() {
  build_browser
}

fix_browser() {
  echo "[browser] Repairing extension build artifacts..."
  rm -rf "${ROOT}/plugins/tce_browser/dist"
  build_browser
}

uninstall_browser() {
  echo "[browser] Removing build artifacts..."
  rm -rf "${ROOT}/plugins/tce_browser/dist"
}

run_action_for_selection() {
  if [ "$SELECT_CLI" = "true" ]; then
    case "$ACTION" in
      install) install_cli ;;
      fix) fix_cli ;;
      uninstall) uninstall_cli ;;
    esac
  fi

  if [ "$SELECT_GIT" = "true" ]; then
    case "$ACTION" in
      install) install_git ;;
      fix) fix_git ;;
      uninstall) uninstall_git ;;
    esac
  fi

  if [ "$SELECT_VSCODE" = "true" ]; then
    case "$ACTION" in
      install) install_vscode ;;
      fix) fix_vscode ;;
      uninstall) uninstall_vscode ;;
    esac
  fi

  if [ "$SELECT_BROWSER" = "true" ]; then
    case "$ACTION" in
      install) install_browser ;;
      fix) fix_browser ;;
      uninstall) uninstall_browser ;;
    esac
  fi
}

confirm_uninstall_if_needed
run_action_for_selection

echo
echo "Plugin action completed: ${ACTION}"
echo "Tip: run ./scripts/doctor.sh auto"
