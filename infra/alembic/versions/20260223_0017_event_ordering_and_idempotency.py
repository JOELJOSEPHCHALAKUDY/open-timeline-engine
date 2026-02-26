"""event ordering and idempotency metadata"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260223_0017"
down_revision = "20260222_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS source_id TEXT")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS source_seq BIGINT")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS vector_clock JSONB NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS idempotency_key TEXT")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS authority_level TEXT NOT NULL DEFAULT 'incidental'")

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_source_seq_scope
        ON events ((context->>'_tce_workspace'), source_id, source_seq)
        WHERE source_id IS NOT NULL AND source_seq IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_idempotency_scope
        ON events ((context->>'_tce_workspace'), (context->>'_tce_owner'), idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )

    op.execute("ALTER TABLE event_identity ADD COLUMN IF NOT EXISTS ingest_seq BIGINT")
    op.execute("CREATE SEQUENCE IF NOT EXISTS event_identity_ingest_seq")
    op.execute("ALTER TABLE event_identity ALTER COLUMN ingest_seq SET DEFAULT nextval('event_identity_ingest_seq')")
    op.execute("UPDATE event_identity SET ingest_seq = nextval('event_identity_ingest_seq') WHERE ingest_seq IS NULL")
    op.execute("ALTER TABLE event_identity ALTER COLUMN ingest_seq SET NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_event_identity_ingest_seq ON event_identity (ingest_seq)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_event_identity_ingest_seq")
    op.execute("ALTER TABLE event_identity DROP COLUMN IF EXISTS ingest_seq")
    op.execute("DROP SEQUENCE IF EXISTS event_identity_ingest_seq")

    op.execute("DROP INDEX IF EXISTS idx_events_idempotency_scope")
    op.execute("DROP INDEX IF EXISTS idx_events_source_seq_scope")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS authority_level")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS idempotency_key")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS vector_clock")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS source_seq")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS source_id")
