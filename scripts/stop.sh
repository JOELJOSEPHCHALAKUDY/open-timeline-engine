#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FULL_COMPOSE="$ROOT/infra/docker-compose.yml"
LITE_COMPOSE="$ROOT/infra/docker-compose.lite.yml"
MODE=""
REMOVE_DATA="false"
REMOVE_IMAGES="false"
PURGE_LOCAL_STATE="false"
CONFIRM_WIPE=""
WIPE_CONFIRM_PHRASE="${TCE_WIPE_CONFIRM_PHRASE:-WIPE TCE DATA}"

usage() {
  cat <<'EOF'
Usage: ./scripts/stop.sh [full|lite|all] [--remove-data] [--confirm-wipe "<phrase>"] [--remove-images] [--purge-local-state]

Examples:
  ./scripts/stop.sh
  ./scripts/stop.sh full
  ./scripts/stop.sh lite --remove-data --confirm-wipe "WIPE TCE DATA"
  ./scripts/stop.sh all --remove-data --confirm-wipe "WIPE TCE DATA" --remove-images --purge-local-state
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    full|--full)
      MODE="full"
      ;;
    lite|--lite)
      MODE="lite"
      ;;
    all|--all)
      MODE="all"
      ;;
    --remove-data|-v)
      REMOVE_DATA="true"
      ;;
    --confirm-wipe)
      CONFIRM_WIPE="${2:-}"
      shift
      ;;
    --remove-images|--rmi-local)
      REMOVE_IMAGES="true"
      ;;
    --purge-local-state)
      PURGE_LOCAL_STATE="true"
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

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required but not found." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose is required but not available." >&2
  exit 1
fi

if [ -z "$MODE" ]; then
  if [ -t 0 ]; then
    echo "Choose stack target:"
    echo "1) Full stack"
    echo "2) Lightweight stack"
    echo "3) Both stacks (default)"
    read -r -p "Enter choice [1/2/3]: " choice
    case "$choice" in
      1|full|Full|FULL)
        MODE="full"
        ;;
      2|lite|Lite|LITE)
        MODE="lite"
        ;;
      *)
        MODE="all"
        ;;
    esac
  else
    MODE="all"
  fi
fi

compose_down() {
  local compose_file="$1"
  local -a args
  args=(down --remove-orphans)
  if [ "$REMOVE_DATA" = "true" ]; then
    args+=(-v)
  fi
  if [ "$REMOVE_IMAGES" = "true" ]; then
    args+=(--rmi local)
  fi
  docker compose -f "$compose_file" "${args[@]}"
}

confirm_destructive_wipe() {
  local typed="$CONFIRM_WIPE"
  if [ -z "$typed" ]; then
    if [ -t 0 ]; then
      echo
      echo "Data-wipe protection:"
      echo "This will permanently delete local timeline data (Postgres/Redis volumes)."
      read -r -p "Type '${WIPE_CONFIRM_PHRASE}' to continue: " typed
    else
      echo "Refusing destructive stop in non-interactive mode without --confirm-wipe \"${WIPE_CONFIRM_PHRASE}\"." >&2
      exit 1
    fi
  fi
  if [ "$typed" != "$WIPE_CONFIRM_PHRASE" ]; then
    echo "Wipe confirmation mismatch. Aborting." >&2
    exit 1
  fi
}

create_pre_wipe_backup() {
  local backup_script="${ROOT}/scripts/db_backup.sh"
  if [ ! -x "$backup_script" ]; then
    echo "Backup helper missing: ${backup_script}. Aborting destructive stop." >&2
    exit 1
  fi
  echo "Creating pre-wipe backup snapshot..."
  "$backup_script" --reason "pre-remove-data"
}

if [ "$REMOVE_DATA" = "true" ]; then
  confirm_destructive_wipe
  create_pre_wipe_backup
fi

if [ "$MODE" = "full" ] || [ "$MODE" = "all" ]; then
  echo "Stopping full stack..."
  compose_down "$FULL_COMPOSE"
fi

if [ "$MODE" = "lite" ] || [ "$MODE" = "all" ]; then
  echo "Stopping lightweight stack..."
  compose_down "$LITE_COMPOSE"
fi

if [ "$PURGE_LOCAL_STATE" = "true" ]; then
  echo "Removing local project and CLI state..."
  rm -f "$ROOT/.env"
  rm -rf "$HOME/.config/open-timeline-engine"
  rm -rf "$HOME/.config/open-timeline-engine/tce/cli_capture"
  rm -rf "$HOME/Library/Application Support/open-timeline-engine"
  rm -rf "$HOME/AppData/Roaming/open-timeline-engine"
fi

echo "Done."
