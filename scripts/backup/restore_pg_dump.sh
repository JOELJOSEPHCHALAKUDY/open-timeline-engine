#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: restore_pg_dump.sh <backup.sql.gz>"
  exit 1
fi

FILE="$1"
if [ ! -f "$FILE" ]; then
  echo "backup file not found: $FILE"
  exit 1
fi

gunzip -c "$FILE" | psql "${TCE_DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/tce}"

echo "restore complete from $FILE"
