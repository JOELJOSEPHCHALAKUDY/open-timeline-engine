# Disaster Recovery Runbook

## Cold Restore

1. Stop stack.
2. Restore latest weekly snapshot or nightly pg_dump.
3. Start `postgres` only and verify schema.
4. Start API and worker.
5. Run integrity check script.
6. Resume MCP and plugin replay queues.

## Recovery Validation

- `/v1/health` returns `ok`
- recent `events` rows are present
- `audit_log` writes new entries
- replay queues can drain
