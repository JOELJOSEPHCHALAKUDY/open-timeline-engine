"""graph entities, team memberships, and fact conflict tracking"""

from __future__ import annotations

from alembic import op

revision = "20260218_0003"
down_revision = "20260218_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_nodes (
          id UUID PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          owner_id TEXT NOT NULL,
          entity_type TEXT NOT NULL,
          entity_key TEXT NOT NULL,
          display_name TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          UNIQUE (workspace_id, owner_id, entity_type, entity_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS event_entity_links (
          id UUID PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          owner_id TEXT NOT NULL,
          event_id UUID NOT NULL REFERENCES event_identity(id) ON DELETE CASCADE,
          entity_id UUID NOT NULL REFERENCES entity_nodes(id) ON DELETE CASCADE,
          role TEXT NOT NULL,
          confidence REAL NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          UNIQUE (workspace_id, event_id, entity_id, role)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS event_relationships (
          id UUID PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          owner_id TEXT NOT NULL,
          source_event_id UUID NOT NULL REFERENCES event_identity(id) ON DELETE CASCADE,
          target_event_id UUID NOT NULL REFERENCES event_identity(id) ON DELETE CASCADE,
          relationship_type TEXT NOT NULL,
          confidence REAL NOT NULL,
          relation_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_assertions (
          id UUID PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          owner_id TEXT NOT NULL,
          domain TEXT NOT NULL,
          fact_key TEXT NOT NULL,
          fact_value TEXT NOT NULL,
          event_id UUID NOT NULL REFERENCES event_identity(id) ON DELETE CASCADE,
          supersedes_event_id UUID NULL REFERENCES event_identity(id) ON DELETE SET NULL,
          active BOOLEAN NOT NULL DEFAULT true,
          created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS team_memberships (
          id UUID PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          role TEXT NOT NULL,
          added_by TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          active BOOLEAN NOT NULL DEFAULT true,
          UNIQUE (workspace_id, user_id)
        )
        """
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_nodes_scope ON entity_nodes (workspace_id, owner_id, updated_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_entity_links_event ON event_entity_links (workspace_id, event_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_relationships_source ON event_relationships (workspace_id, source_event_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_fact_assertions_active ON fact_assertions (workspace_id, owner_id, fact_key, active)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_team_memberships_workspace ON team_memberships (workspace_id, created_at ASC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS team_memberships")
    op.execute("DROP TABLE IF EXISTS fact_assertions")
    op.execute("DROP TABLE IF EXISTS event_relationships")
    op.execute("DROP TABLE IF EXISTS event_entity_links")
    op.execute("DROP TABLE IF EXISTS entity_nodes")
