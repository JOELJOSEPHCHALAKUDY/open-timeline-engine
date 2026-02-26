"""episodes core tables"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260223_0018"
down_revision = "20260223_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT 'default',
            goal TEXT NOT NULL,
            context TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'open',
            authority_score REAL NOT NULL DEFAULT 0.0,
            stability_score REAL NOT NULL DEFAULT 0.0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS episode_decisions (
            id UUID PRIMARY KEY,
            episode_id UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            decision TEXT NOT NULL,
            why TEXT NOT NULL DEFAULT '',
            alternatives_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS episode_lessons (
            episode_id UUID PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
            do_more_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            do_less_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            avoid_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS episode_event_links (
            id UUID PRIMARY KEY,
            episode_id UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            event_id UUID NOT NULL REFERENCES event_identity(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_episodes_scope_updated
        ON episodes (workspace_id, user_id, session_id, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_episode_decisions_episode
        ON episode_decisions (episode_id, created_at ASC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_episode_event_links_scope
        ON episode_event_links (episode_id, event_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_episode_event_links_scope")
    op.execute("DROP INDEX IF EXISTS idx_episode_decisions_episode")
    op.execute("DROP INDEX IF EXISTS idx_episodes_scope_updated")
    op.execute("DROP TABLE IF EXISTS episode_event_links")
    op.execute("DROP TABLE IF EXISTS episode_lessons")
    op.execute("DROP TABLE IF EXISTS episode_decisions")
    op.execute("DROP TABLE IF EXISTS episodes")
