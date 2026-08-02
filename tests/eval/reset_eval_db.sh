#!/usr/bin/env bash
set -euo pipefail

REPO="/Users/joeljoseph/Desktop/Personal Work/ai-experiments-apps/open-timeline-engine"
evalc() {
  docker compose -p tce-eval \
    -f "$REPO/infra/docker-compose.yml" \
    -f "$REPO/tests/eval/docker-compose.eval.override.yml" "$@"
}

evalc stop tce-api tce-migrate >/dev/null 2>&1 || true
for _ in {1..60}; do
  if [[ "$(docker exec tce-eval-redis-1 redis-cli ping 2>/dev/null || true)" == "PONG" ]]; then
    break
  fi
  sleep 1
done
[[ "$(docker exec tce-eval-redis-1 redis-cli ping 2>/dev/null || true)" == "PONG" ]]
docker exec tce-eval-postgres-1 psql -U postgres -d postgres \
  -c "DROP DATABASE IF EXISTS tce_eval_10k WITH (FORCE);" \
  -c "CREATE DATABASE tce_eval_10k TEMPLATE tce_eval_seed;"
[[ "$(docker exec tce-eval-redis-1 redis-cli -n 15 FLUSHDB)" == "OK" ]]
docker exec tce-eval-postgres-1 psql -U postgres -d tce_eval_10k -tAc \
  "SELECT count(*) FROM events;"
