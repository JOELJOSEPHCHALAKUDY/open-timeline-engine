#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT}/infra/docker-compose.yml"
BACKUP_DIR_MANUAL="${TCE_BACKUP_DIR:-${ROOT}/backups/manual}"
BACKUP_DIR_NIGHTLY="${ROOT}/backups/nightly"
ASSUME_YES="false"
BACKUP_FILE=""
PRINT_LATEST="false"
RESTORE_CONFIRM_PHRASE="${TCE_RESTORE_CONFIRM_PHRASE:-RESTORE TCE DATA}"
QDRANT_BACKUP_FILE=""

usage() {
  cat <<'EOF'
Usage: ./scripts/db_restore.sh [--latest | --file <path>] [--yes] [--print-latest]

Options:
  --latest         Restore from newest backup under backups/manual or backups/nightly (default).
  --file <path>    Restore from a specific .sql.gz backup file.
  --yes            Skip interactive prompt. Requires TCE_RESTORE_CONFIRM_TOKEN to match phrase.
  --print-latest   Print latest backup path and exit.

Note:
  If a matching Qdrant backup file exists
  (<same-name>.qdrant.tar.gz), it is restored too.
EOF
}

latest_backup_file() {
  local candidate
  candidate="$(
    {
      ls -1t "${BACKUP_DIR_MANUAL}"/tce_*.sql.gz 2>/dev/null || true
      ls -1t "${BACKUP_DIR_NIGHTLY}"/tce_*.sql.gz 2>/dev/null || true
    } | head -n 1
  )"
  printf '%s' "$candidate"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --latest)
      ;;
    --file)
      BACKUP_FILE="${2:-}"
      if [ -z "$BACKUP_FILE" ]; then
        echo "--file requires a path." >&2
        exit 1
      fi
      shift
      ;;
    --yes|-y)
      ASSUME_YES="true"
      ;;
    --print-latest)
      PRINT_LATEST="true"
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

if [ "$PRINT_LATEST" = "true" ]; then
  latest="$(latest_backup_file)"
  if [ -z "$latest" ]; then
    exit 1
  fi
  printf '%s\n' "$latest"
  exit 0
fi

if [ -z "$BACKUP_FILE" ]; then
  BACKUP_FILE="$(latest_backup_file)"
fi

if [ -z "$BACKUP_FILE" ]; then
  echo "No backup file found. Create one with ./scripts/db_backup.sh first." >&2
  exit 1
fi
if [ ! -f "$BACKUP_FILE" ]; then
  echo "Backup file not found: $BACKUP_FILE" >&2
  exit 1
fi

if [[ "$BACKUP_FILE" == *.sql.gz ]]; then
  QDRANT_BACKUP_FILE="${BACKUP_FILE%.sql.gz}.qdrant.tar.gz"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required but not found." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose is required but not available." >&2
  exit 1
fi

if [ "$ASSUME_YES" = "true" ]; then
  if [ "${TCE_RESTORE_CONFIRM_TOKEN:-}" != "$RESTORE_CONFIRM_PHRASE" ]; then
    echo "Non-interactive restore requires TCE_RESTORE_CONFIRM_TOKEN='${RESTORE_CONFIRM_PHRASE}'." >&2
    exit 1
  fi
else
  if [ ! -t 0 ]; then
    echo "Interactive confirmation required. Re-run with a TTY, or use --yes and TCE_RESTORE_CONFIRM_TOKEN." >&2
    exit 1
  fi
  echo
  echo "Restore warning:"
  echo "This will replace the current Postgres database contents."
  echo "Backup file: ${BACKUP_FILE}"
  read -r -p "Type '${RESTORE_CONFIRM_PHRASE}' to continue: " restore_confirm
  if [ "$restore_confirm" != "$RESTORE_CONFIRM_PHRASE" ]; then
    echo "Restore cancelled."
    exit 0
  fi
fi

echo "Starting postgres for restore..."
docker compose -f "$COMPOSE_FILE" up -d postgres >/dev/null

for _ in $(seq 1 40); do
  if docker compose -f "$COMPOSE_FILE" exec -T postgres pg_isready -U postgres -d tce >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! docker compose -f "$COMPOSE_FILE" exec -T postgres pg_isready -U postgres -d tce >/dev/null 2>&1; then
  echo "Postgres is not ready; restore aborted." >&2
  exit 1
fi

echo "Resetting database schema..."
docker compose -f "$COMPOSE_FILE" exec -T postgres psql -U postgres -d tce -v ON_ERROR_STOP=1 -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO postgres; GRANT ALL ON SCHEMA public TO public;" >/dev/null
docker compose -f "$COMPOSE_FILE" exec -T postgres psql -U postgres -d tce -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null

echo "Restoring from backup..."
gunzip -c "$BACKUP_FILE" | docker compose -f "$COMPOSE_FILE" exec -T postgres psql -U postgres -d tce -v ON_ERROR_STOP=1 >/dev/null

echo "Applying migrations..."
docker compose -f "$COMPOSE_FILE" run --rm tce-migrate >/dev/null

if [ -n "$QDRANT_BACKUP_FILE" ] && [ -f "$QDRANT_BACKUP_FILE" ]; then
  echo "Restoring Qdrant from ${QDRANT_BACKUP_FILE} ..."
  docker compose -f "$COMPOSE_FILE" up -d qdrant >/dev/null

  qdrant_container_id="$(docker compose -f "$COMPOSE_FILE" ps -q qdrant 2>/dev/null || true)"
  if [ -z "$qdrant_container_id" ]; then
    echo "Qdrant container not found; restore aborted." >&2
    exit 1
  fi

  qdrant_volume_name="$(docker inspect "$qdrant_container_id" --format '{{range .Mounts}}{{if eq .Destination "/qdrant/storage"}}{{.Name}}{{end}}{{end}}' 2>/dev/null || true)"
  if [ -z "$qdrant_volume_name" ]; then
    echo "Qdrant storage volume not found; restore aborted." >&2
    exit 1
  fi

  docker compose -f "$COMPOSE_FILE" stop qdrant >/dev/null
  qdrant_backup_dir="$(cd "$(dirname "$QDRANT_BACKUP_FILE")" && pwd)"
  qdrant_backup_name="$(basename "$QDRANT_BACKUP_FILE")"
  docker run --rm \
    -v "${qdrant_volume_name}:/to" \
    -v "${qdrant_backup_dir}:/backup:ro" \
    alpine:3.20 \
    sh -c "find /to -mindepth 1 -delete && tar -xzf /backup/${qdrant_backup_name} -C /to" >/dev/null
  docker compose -f "$COMPOSE_FILE" up -d qdrant >/dev/null
else
  echo "No matching Qdrant backup found; leaving current Qdrant data unchanged."
fi

echo "Restore complete from ${BACKUP_FILE}"
