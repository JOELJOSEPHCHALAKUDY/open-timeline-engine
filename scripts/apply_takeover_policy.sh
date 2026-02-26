#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_ROOT="${PWD}"
COPY_POLICY="false"
OPEN_CLAUDE="false"

POLICY_FILE="$ROOT/docs/mcp-config/generated/takeover_auto_call_policy.md"
START_MARKER="# >>> TCE TAKEOVER POLICY >>>"
END_MARKER="# <<< TCE TAKEOVER POLICY <<<"

usage() {
  cat <<'EOF'
Usage: ./scripts/apply_takeover_policy.sh [options]

Options:
  --workspace-root <path>  Workspace root where AGENTS.md / CLAUDE.md will be updated (default: current directory)
  --copy-policy            Copy takeover policy text to clipboard (if supported)
  --open-claude            Open Claude app/settings location helper
  -h, --help

Examples:
  ./scripts/apply_takeover_policy.sh --workspace-root "$PWD" --copy-policy --open-claude
  ./scripts/apply_takeover_policy.sh --workspace-root "/path/to/workspace"
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace-root)
      WORKSPACE_ROOT="${2:-$WORKSPACE_ROOT}"
      shift
      ;;
    --copy-policy)
      COPY_POLICY="true"
      ;;
    --open-claude)
      OPEN_CLAUDE="true"
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

if [ ! -d "$WORKSPACE_ROOT" ]; then
  echo "Workspace root does not exist: $WORKSPACE_ROOT" >&2
  exit 1
fi

if [ ! -f "$POLICY_FILE" ]; then
  echo "Missing policy file: $POLICY_FILE" >&2
  echo "Run ./scripts/configure_mcp_clients.sh --client all first." >&2
  exit 1
fi

policy_block() {
  cat <<'EOF'
# >>> TCE TAKEOVER POLICY >>>
# Compatibility marker for setup tooling.
# Authoritative takeover policy source:
#   open-timeline-engine/docs/mcp-config/generated/takeover_auto_call_policy.md
# Keep this marker block present for idempotent script behavior.
# Do not duplicate policy rules in this block.
# <<< TCE TAKEOVER POLICY <<<
EOF
}

append_policy_block_if_missing() {
  local file_path="$1"
  if [ -f "$file_path" ] && grep -Fq "$START_MARKER" "$file_path"; then
    return
  fi
  {
    if [ -f "$file_path" ]; then
      echo
    fi
    policy_block
  } >> "$file_path"
}

workspace_agents="$WORKSPACE_ROOT/AGENTS.md"
workspace_claude="$WORKSPACE_ROOT/CLAUDE.md"

append_policy_block_if_missing "$workspace_agents"
append_policy_block_if_missing "$workspace_claude"

echo "Applied takeover policy block:"
echo "  - $workspace_agents"
echo "  - $workspace_claude"

if [ "$COPY_POLICY" = "true" ]; then
  if command -v pbcopy >/dev/null 2>&1; then
    cat "$POLICY_FILE" | pbcopy
    echo "Copied policy to clipboard with pbcopy."
  elif command -v wl-copy >/dev/null 2>&1; then
    cat "$POLICY_FILE" | wl-copy
    echo "Copied policy to clipboard with wl-copy."
  elif command -v xclip >/dev/null 2>&1; then
    xclip -selection clipboard < "$POLICY_FILE"
    echo "Copied policy to clipboard with xclip."
  elif command -v clip >/dev/null 2>&1; then
    clip < "$POLICY_FILE"
    echo "Copied policy to clipboard with clip."
  else
    echo "No clipboard command found (pbcopy/wl-copy/xclip/clip)."
  fi
fi

if [ "$OPEN_CLAUDE" = "true" ]; then
  if [ "$(uname -s)" = "Darwin" ]; then
    open -a Claude >/dev/null 2>&1 || true
    open "$HOME/Library/Application Support/Claude" >/dev/null 2>&1 || true
    echo "Opened Claude app/settings folder helper."
  else
    echo "Open Claude Desktop settings manually and paste clipboard policy into Custom Instructions."
  fi
fi

echo "Done."
