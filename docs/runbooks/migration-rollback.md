# Migration Rollback Plan

1. Stop API and worker writes.
2. Restore latest validated DB backup.
3. Run `alembic -c infra/alembic.ini downgrade -1` only for safe reversible changes.
4. Re-run migration smoke in staging-like local env.
5. Resume services and run integrity checks.

## Events Partitioning v2 notes

- Migration `20260218_0002` moves `events` to partitioned storage.
- Legacy table is retained as `events_unpartitioned_legacy` for rollback safety.
- Rollback path renames legacy table back to `events` and drops partitioned copy.
