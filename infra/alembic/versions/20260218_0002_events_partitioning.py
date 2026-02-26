"""events partitioning v2 with identity guard"""

from __future__ import annotations

from alembic import op

revision = "20260218_0002"
down_revision = "20260218_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS events_v2 (
          id UUID NOT NULL,
          ts TIMESTAMPTZ NOT NULL,
          actor TEXT NOT NULL,
          source TEXT NOT NULL,
          domain TEXT NOT NULL,
          task_type TEXT NOT NULL,
          event_type TEXT NOT NULL,
          title TEXT NOT NULL,
          payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          context JSONB NOT NULL DEFAULT '{}'::jsonb,
          inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
          steps JSONB NOT NULL DEFAULT '[]'::jsonb,
          decision JSONB NULL,
          outcome JSONB NULL,
          style JSONB NULL,
          links JSONB NULL,
          tags JSONB NOT NULL DEFAULT '[]'::jsonb,
          sensitivity SMALLINT NOT NULL DEFAULT 1,
          redaction_hints JSONB NOT NULL DEFAULT '[]'::jsonb,
          redaction_policy_id UUID NULL,
          hash TEXT NOT NULL,
          schema_version INT NOT NULL DEFAULT 1,
          PRIMARY KEY (id, ts)
        ) PARTITION BY RANGE (ts)
        """
    )

    op.execute(
        """
        DO $$
        DECLARE
          start_month DATE := (date_trunc('month', now()) - interval '1 month')::date;
          cur_month DATE := start_month;
          next_month DATE;
          i INT := 0;
          part_name TEXT;
        BEGIN
          WHILE i < 4 LOOP
            next_month := (cur_month + interval '1 month')::date;
            part_name := format('events_v2_%s', to_char(cur_month, 'YYYYMM'));
            EXECUTE format(
              'CREATE TABLE IF NOT EXISTS %I PARTITION OF events_v2 FOR VALUES FROM (%L) TO (%L)',
              part_name,
              cur_month,
              next_month
            );
            cur_month := next_month;
            i := i + 1;
          END LOOP;

          EXECUTE 'CREATE TABLE IF NOT EXISTS events_v2_default PARTITION OF events_v2 DEFAULT';
        END $$;
        """
    )

    op.execute("CREATE INDEX IF NOT EXISTS idx_events_v2_ts ON events_v2 (ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_events_v2_domain_task_type_ts ON events_v2 (domain, task_type, ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_events_v2_payload_gin ON events_v2 USING GIN (payload)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_events_v2_id ON events_v2 (id)")

    op.execute(
        """
        INSERT INTO events_v2 (
          id, ts, actor, source, domain, task_type, event_type, title,
          payload, context, inputs, steps, decision, outcome, style, links,
          tags, sensitivity, redaction_hints, redaction_policy_id, hash, schema_version
        )
        SELECT
          id, ts, actor, source, domain, task_type, event_type, title,
          payload, context, inputs, steps, decision, outcome, style, links,
          tags, sensitivity, redaction_hints, redaction_policy_id, hash, schema_version
        FROM events
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS event_identity (
          id UUID PRIMARY KEY,
          ts TIMESTAMPTZ NOT NULL,
          CONSTRAINT fk_event_identity_events
            FOREIGN KEY (id, ts)
            REFERENCES events_v2(id, ts)
            ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        INSERT INTO event_identity (id, ts)
        SELECT id, ts
        FROM events_v2
        ON CONFLICT (id) DO NOTHING
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION tce_track_event_identity()
        RETURNS trigger AS $$
        BEGIN
          INSERT INTO event_identity (id, ts)
          VALUES (NEW.id, NEW.ts);
          RETURN NEW;
        EXCEPTION WHEN unique_violation THEN
          RAISE EXCEPTION 'duplicate event id %', NEW.id;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_events_identity
        AFTER INSERT ON events_v2
        FOR EACH ROW
        EXECUTE FUNCTION tce_track_event_identity()
        """
    )

    op.execute("ALTER TABLE event_embeddings DROP CONSTRAINT IF EXISTS event_embeddings_event_id_fkey")
    op.execute("ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS artifacts_event_id_fkey")

    op.execute("ALTER TABLE events RENAME TO events_unpartitioned_legacy")
    op.execute("ALTER TABLE events_v2 RENAME TO events")
    op.execute("ALTER TABLE IF EXISTS events_v2_default RENAME TO events_default")

    op.execute(
        """
        ALTER TABLE event_embeddings
        ADD CONSTRAINT event_embeddings_event_id_fkey
        FOREIGN KEY (event_id)
        REFERENCES event_identity(id)
        ON DELETE CASCADE
        """
    )

    op.execute(
        """
        ALTER TABLE artifacts
        ADD CONSTRAINT artifacts_event_id_fkey
        FOREIGN KEY (event_id)
        REFERENCES event_identity(id)
        ON DELETE CASCADE
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE event_embeddings DROP CONSTRAINT IF EXISTS event_embeddings_event_id_fkey")
    op.execute("ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS artifacts_event_id_fkey")

    op.execute("DROP TRIGGER IF EXISTS trg_events_identity ON events")
    op.execute("DROP FUNCTION IF EXISTS tce_track_event_identity()")

    op.execute("ALTER TABLE events RENAME TO events_partitioned_legacy")
    op.execute("ALTER TABLE events_unpartitioned_legacy RENAME TO events")

    op.execute("DROP TABLE IF EXISTS event_identity")

    op.execute(
        """
        ALTER TABLE event_embeddings
        ADD CONSTRAINT event_embeddings_event_id_fkey
        FOREIGN KEY (event_id)
        REFERENCES events(id)
        ON DELETE CASCADE
        """
    )

    op.execute(
        """
        ALTER TABLE artifacts
        ADD CONSTRAINT artifacts_event_id_fkey
        FOREIGN KEY (event_id)
        REFERENCES events(id)
        ON DELETE CASCADE
        """
    )

    op.execute("DROP TABLE IF EXISTS events_partitioned_legacy CASCADE")
