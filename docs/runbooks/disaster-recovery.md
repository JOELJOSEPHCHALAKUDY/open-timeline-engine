# Disaster Recovery Runbook

Restore uses `scripts/db_restore.sh`, which recovers the Postgres and Qdrant
named volumes from artifacts produced by `scripts/db_backup.sh`. See
[backup-restore.md](backup-restore.md) for how backups are produced and
scheduled.

## Preconditions

- A backup artifact exists under `backups/manual/` (or `backups/auto/`).
  Confirm before proceeding:

  ```bash
  ./scripts/db_restore.sh --print-latest
  ```

  If nothing is listed, there is no restore point — recovery is not possible.
  Ensure nightly backups are enabled (`TCE_ENABLE_NIGHTLY_BACKUP=1`) going
  forward.

## Cold Restore

1. Stop the stack: `./scripts/stop.sh full`.
2. Restore the latest backup (recreates volumes, runs `pg_restore` and the
   Qdrant volume untar):

   ```bash
   ./scripts/db_restore.sh --latest --yes
   ```

3. Bring the stack up: `./scripts/start.sh full` — migrations run and the API,
   worker, and MCP services start.
4. Run the integrity check: `python scripts/integrity/check_hashes.py`.
5. Resume MCP and plugin replay queues.

## Recovery Validation

- `/v1/health` returns `ok`
- recent `events` rows are present
- `audit_log` writes new entries
- replay queues can drain
