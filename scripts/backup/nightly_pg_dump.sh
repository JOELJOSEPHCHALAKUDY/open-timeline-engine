#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${TCE_BACKUP_DIR:-./backups/nightly}"
mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"

pg_dump "${TCE_DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/tce}" | gzip > "$BACKUP_DIR/tce_${STAMP}.sql.gz"

echo "nightly backup written: $BACKUP_DIR/tce_${STAMP}.sql.gz"
