#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT}/infra/docker-compose.yml"
BACKUP_DIR="${TCE_BACKUP_DIR:-${ROOT}/backups/manual}"
REASON="manual"

while [ $# -gt 0 ]; do
  case "$1" in
    --reason)
      REASON="${2:-manual}"
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: ./scripts/db_backup.sh [--reason <label>]

Creates backup artifacts from the full stack into:
  backups/manual/tce_<timestamp>_<reason>.sql.gz
  backups/manual/tce_<timestamp>_<reason>.qdrant.tar.gz
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
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

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%d_%H%M%S)"
safe_reason="$(printf '%s' "$REASON" | tr -cs 'a-zA-Z0-9_.-' '_')"
out_file="${BACKUP_DIR}/tce_${stamp}_${safe_reason}.sql.gz"
qdrant_file="${BACKUP_DIR}/tce_${stamp}_${safe_reason}.qdrant.tar.gz"

if ! docker compose -f "$COMPOSE_FILE" ps --services --filter status=running | grep -qx "postgres"; then
  echo "Starting postgres for backup..."
  docker compose -f "$COMPOSE_FILE" up -d postgres >/dev/null
fi

for _ in $(seq 1 40); do
  if docker compose -f "$COMPOSE_FILE" exec -T postgres pg_isready -U postgres -d tce >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! docker compose -f "$COMPOSE_FILE" exec -T postgres pg_isready -U postgres -d tce >/dev/null 2>&1; then
  echo "Postgres is not ready; backup aborted." >&2
  exit 1
fi

docker compose -f "$COMPOSE_FILE" exec -T postgres pg_dump -U postgres -d tce | gzip -c > "$out_file"

if ! docker compose -f "$COMPOSE_FILE" ps --services --filter status=running | grep -qx "qdrant"; then
  echo "Starting qdrant for backup..."
  docker compose -f "$COMPOSE_FILE" up -d qdrant >/dev/null
fi

qdrant_container_id="$(docker compose -f "$COMPOSE_FILE" ps -q qdrant 2>/dev/null || true)"
if [ -z "$qdrant_container_id" ]; then
  echo "Qdrant container not found; backup aborted." >&2
  exit 1
fi

qdrant_volume_name="$(docker inspect "$qdrant_container_id" --format '{{range .Mounts}}{{if eq .Destination "/qdrant/storage"}}{{.Name}}{{end}}{{end}}' 2>/dev/null || true)"
if [ -z "$qdrant_volume_name" ]; then
  echo "Qdrant storage volume not found; backup aborted." >&2
  exit 1
fi

qdrant_file_name="$(basename "$qdrant_file")"
docker run --rm \
  -v "${qdrant_volume_name}:/from:ro" \
  -v "${BACKUP_DIR}:/to" \
  alpine:3.20 \
  sh -c "cd /from && tar -czf /to/${qdrant_file_name} ." >/dev/null

echo "Postgres backup written: ${out_file}"
echo "Qdrant backup written: ${qdrant_file}"
printf '%s\n' "$out_file"
