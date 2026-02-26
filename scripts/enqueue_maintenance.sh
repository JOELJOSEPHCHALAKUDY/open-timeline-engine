#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FULL_COMPOSE="${ROOT}/infra/docker-compose.yml"

if [ -f "${ROOT}/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "${ROOT}/.env"
  set +a
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  running_services="$(docker compose -f "${FULL_COMPOSE}" ps --status running --services 2>/dev/null || true)"
  if printf '%s\n' "${running_services}" | grep -Eq '^tce-worker$'; then
    docker compose -f "${FULL_COMPOSE}" exec -T tce-worker python -m tce_worker.scheduler
    exit 0
  fi
fi

if command -v python3 >/dev/null 2>&1; then
  PYTHONPATH="${ROOT}/services/tce_worker" python3 -m tce_worker.scheduler
  exit 0
fi

echo "Unable to enqueue maintenance jobs: no running tce-worker container and python3 not found." >&2
exit 1
