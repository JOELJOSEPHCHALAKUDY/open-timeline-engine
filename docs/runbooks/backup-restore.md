# Backup and Restore

## Nightly backup

Use:

```bash
./scripts/backup/nightly_pg_dump.sh
```

## Weekly snapshot

Use:

```bash
./scripts/backup/weekly_snapshot.sh
```

## Restore from pg_dump

Use:

```bash
./scripts/backup/restore_pg_dump.sh ./backups/nightly/tce_YYYYMMDD_HHMMSS.sql.gz
```

## Post-restore checks

- `curl http://localhost:8080/v1/health`
- `python scripts/integrity/check_hashes.py`
