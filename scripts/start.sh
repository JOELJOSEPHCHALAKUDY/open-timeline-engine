#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FULL_COMPOSE="$ROOT/infra/docker-compose.yml"
LITE_COMPOSE="$ROOT/infra/docker-compose.lite.yml"
MODE=""
DETACH="false"

usage() {
  cat <<'EOF'
Usage: ./scripts/start.sh [full|lite] [--detach]

If no mode is passed, the script asks interactively and defaults to full.
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
    -d|--detach)
      DETACH="true"
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
    echo "Choose runtime:"
    echo "1) Full production stack (default)"
    echo "2) Lightweight stack"
    read -r -p "Enter choice [1/2]: " choice
    case "$choice" in
      2|lite|Lite|LITE)
        MODE="lite"
        ;;
      *)
        MODE="full"
        ;;
    esac
  else
    MODE="full"
  fi
fi

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
fi

if [ "$MODE" = "lite" ]; then
  COMPOSE_FILE="$LITE_COMPOSE"
else
  COMPOSE_FILE="$FULL_COMPOSE"
fi

# The host-capture credential never lives in .env (that file is mounted into the API
# container and readable from any executor shell). Compose bind-mounts the 0600 token file
# read-only at /run/secrets/tce_host_capture_token instead; docker would create a
# root-owned DIRECTORY if the source were missing, so make sure the file exists first.
# An empty file simply means host capture stays disabled until ./scripts/install.sh runs.
TCE_HOST_CAPTURE_TOKEN_FILE="${TCE_HOST_CAPTURE_TOKEN_FILE:-${XDG_CONFIG_HOME:-${HOME}/.config}/open-timeline-engine/host_capture.token}"
if [ ! -f "$TCE_HOST_CAPTURE_TOKEN_FILE" ]; then
  (umask 077 && mkdir -p "$(dirname "$TCE_HOST_CAPTURE_TOKEN_FILE")" && : > "$TCE_HOST_CAPTURE_TOKEN_FILE") || true
  chmod 600 "$TCE_HOST_CAPTURE_TOKEN_FILE" 2>/dev/null || true
fi
export TCE_HOST_CAPTURE_TOKEN_FILE

echo "Starting mode: $MODE"
echo "Compose file: $COMPOSE_FILE"

if [ "$MODE" = "full" ]; then
  echo "Ensuring database is ready for migrations ..."
  docker compose -f "$COMPOSE_FILE" up -d postgres
  echo "Applying DB migrations (alembic upgrade head) ..."
  docker compose -f "$COMPOSE_FILE" run --rm --build tce-migrate
fi

if [ "$DETACH" = "true" ]; then
  docker compose -f "$COMPOSE_FILE" up -d --build
else
  docker compose -f "$COMPOSE_FILE" up --build
fi

if [ "$MODE" = "full" ]; then
  embed_model="${TCE_EMBED_MODEL:-mxbai-embed-large}"
  if [ -f "$ROOT/.env" ]; then
    env_model="$(awk -F= '/^TCE_EMBED_MODEL=/ {print $2}' "$ROOT/.env")"
    if [ -n "$env_model" ]; then
      embed_model="$env_model"
    fi
  fi
  echo "Pulling Ollama embedding model: ${embed_model} ..."
  if docker compose -f "$COMPOSE_FILE" exec -T ollama ollama pull "$embed_model" >/dev/null 2>&1; then
    echo "Ollama model ready: ${embed_model}"
  else
    echo "Warning: failed to pull Ollama model '${embed_model}'. Vector search will fall back to lexical-only."
  fi
fi

echo
echo "API: http://localhost:8080"
if [ "$MODE" = "full" ]; then
  echo "Prometheus: http://localhost:9090"
  echo "Grafana: http://localhost:3000"
fi
