#!/usr/bin/env bash
set -euo pipefail

SNAPSHOT_DIR="${TCE_SNAPSHOT_DIR:-./backups/weekly}"
mkdir -p "$SNAPSHOT_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$SNAPSHOT_DIR/tce_pgdata_${STAMP}.tar.gz"

if [ -d "./infra/data/postgres" ]; then
  tar -czf "$OUT" -C "./infra/data" postgres
  echo "weekly snapshot written: $OUT"
else
  echo "./infra/data/postgres not found; snapshot skipped"
fi
