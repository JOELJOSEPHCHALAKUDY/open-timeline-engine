# Backup and Restore

Backups use `scripts/db_backup.sh` / `scripts/db_restore.sh`, which operate on
the full stack's **named Docker volumes** (`postgres_data`, `qdrant_data`).

> The older `scripts/backup/weekly_snapshot.sh` and `nightly_pg_dump.sh` are
> deprecated: `weekly_snapshot.sh` targets a bind-mount path the compose stack
> does not use, and nothing schedules `nightly_pg_dump.sh`. Use the scripts
> below instead.

## Manual backup

```bash
./scripts/db_backup.sh --reason manual
```

Artifacts are written to:

- `backups/manual/tce_<timestamp>_<reason>.sql.gz` (Postgres `pg_dump`)
- `backups/manual/tce_<timestamp>_<reason>.qdrant.tar.gz` (Qdrant volume)

Backups are also taken automatically before destructive operations
(`install.sh restart`, `stop.sh --remove-data`).

## Scheduled nightly backup (opt-in)

Enable during install by exporting the opt-in flag:

```bash
TCE_ENABLE_NIGHTLY_BACKUP=1 ./scripts/install.sh install full
```

This installs a cron entry (default `30 2 * * *`) that runs `db_backup.sh` and
prunes artifacts older than `TCE_BACKUP_RETENTION_DAYS` (default 14). Override
the schedule with `TCE_BACKUP_CRON`.

## Restore

```bash
./scripts/db_restore.sh --latest
```

Or restore a specific artifact:

```bash
./scripts/db_restore.sh --file backups/manual/tce_<timestamp>_<reason>.sql.gz
```

## Post-restore checks

- `curl http://localhost:8080/v1/health` returns `ok`
- `python scripts/integrity/check_hashes.py`
- recent `events` rows are present
