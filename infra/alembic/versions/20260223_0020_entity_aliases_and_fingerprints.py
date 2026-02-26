"""entity aliases and event fingerprints"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260223_0020"
down_revision = "20260223_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_aliases (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            canonical_entity_id UUID NOT NULL,
            alias_key TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS event_fingerprints (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            event_id UUID NOT NULL REFERENCES event_identity(id) ON DELETE CASCADE,
            fingerprint_hash TEXT NOT NULL,
            fingerprint_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_entity_aliases_scope_key
        ON entity_aliases (workspace_id, owner_id, alias_key)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_fingerprints_scope_hash
        ON event_fingerprints (workspace_id, owner_id, fingerprint_hash, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_event_fingerprints_scope_hash")
    op.execute("DROP INDEX IF EXISTS idx_entity_aliases_scope_key")
    op.execute("DROP TABLE IF EXISTS event_fingerprints")
    op.execute("DROP TABLE IF EXISTS entity_aliases")
