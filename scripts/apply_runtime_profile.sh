#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_DIR="${ROOT}/config/profiles"
ENV_FILE="${ROOT}/.env"
PROFILE=""
OVERWRITE="false"

usage() {
  cat <<'EOF'
Usage: ./scripts/apply_runtime_profile.sh --profile <name> [--env-file <path>] [--overwrite]

Profiles: local-lite, local-full, team-secure, research

By default, existing keys are preserved. --overwrite replaces keys owned by the
selected profile; keys not present in the profile are never removed.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --profile)
      PROFILE="${2:-}"
      shift
      ;;
    --env-file)
      ENV_FILE="${2:-}"
      shift
      ;;
    --overwrite)
      OVERWRITE="true"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

case "$PROFILE" in
  local-lite|local-full|team-secure|research)
    ;;
  *)
    echo "Invalid runtime profile: ${PROFILE:-<empty>}" >&2
    usage >&2
    exit 2
    ;;
esac

PROFILE_FILE="${PROFILE_DIR}/${PROFILE}.env"
if [ ! -f "$PROFILE_FILE" ]; then
  echo "Runtime profile file not found: ${PROFILE_FILE}" >&2
  exit 1
fi

mkdir -p "$(dirname "$ENV_FILE")"
touch "$ENV_FILE"

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

while IFS= read -r raw_line || [ -n "$raw_line" ]; do
  line="${raw_line#"${raw_line%%[![:space:]]*}"}"
  case "$line" in
    ""|\#*)
      continue
      ;;
  esac

  key="${line%%=*}"
  value="${line#*=}"
  if ! printf '%s' "$key" | grep -Eq '^[A-Z][A-Z0-9_]*$'; then
    echo "Invalid key in ${PROFILE_FILE}: ${key}" >&2
    exit 1
  fi
  if [ "$OVERWRITE" = "true" ] || ! grep -q "^${key}=" "$ENV_FILE"; then
    set_env_key "$key" "$value"
  fi
done < "$PROFILE_FILE"

echo "Applied runtime profile '${PROFILE}' to ${ENV_FILE} (overwrite=${OVERWRITE})."
