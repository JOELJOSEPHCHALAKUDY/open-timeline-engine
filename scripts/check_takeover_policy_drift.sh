#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_ROOT="$(cd "$ROOT/.." && pwd)"
START_MARKER="# >>> TCE TAKEOVER POLICY >>>"
END_MARKER="# <<< TCE TAKEOVER POLICY <<<"

FILES=(
  "$WORKSPACE_ROOT/AGENTS.md"
  "$WORKSPACE_ROOT/CLAUDE.md"
  "$ROOT/AGENTS.md"
  "$ROOT/CLAUDE.md"
)

normalize_policy() {
  local file_path="$1"
  awk -v start="$START_MARKER" -v end="$END_MARKER" '
    $0 == start { skip = 1; next }
    $0 == end   { skip = 0; next }
    !skip { print }
  ' "$file_path" | sed -E 's/[[:space:]]+$//'
}

for file_path in "${FILES[@]}"; do
  if [ ! -f "$file_path" ]; then
    echo "Missing policy file: $file_path" >&2
    exit 1
  fi
done

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

base_file="${FILES[0]}"
base_norm="$tmpdir/base.norm"
normalize_policy "$base_file" > "$base_norm"

for file_path in "${FILES[@]:1}"; do
  current_norm="$tmpdir/current.norm"
  normalize_policy "$file_path" > "$current_norm"
  if ! cmp -s "$base_norm" "$current_norm"; then
    echo "Takeover policy drift detected:" >&2
    echo "  base:    $base_file" >&2
    echo "  current: $file_path" >&2
    echo >&2
    diff -u "$base_norm" "$current_norm" || true
    exit 1
  fi
done

echo "Takeover policy files are aligned (compatibility marker block ignored)."
