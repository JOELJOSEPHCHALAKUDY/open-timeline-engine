"""initial schema"""

from __future__ import annotations

from alembic import op

revision = "20260218_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
          id UUID PRIMARY KEY,
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
          schema_version INT NOT NULL DEFAULT 1
        )
        """
    )

    op.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_events_domain_task_type_ts ON events (domain, task_type, ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_events_payload_gin ON events USING GIN (payload)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS event_embeddings (
          event_id UUID PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
          embedding vector(1024) NOT NULL,
          model TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_embeddings_hnsw
        ON event_embeddings USING hnsw (embedding vector_cosine_ops)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
          id UUID PRIMARY KEY,
          event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
          kind TEXT NOT NULL,
          uri TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          meta JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS patterns (
          id UUID PRIMARY KEY,
          domain TEXT NOT NULL,
          pattern_type TEXT NOT NULL,
          statement TEXT NOT NULL,
          evidence_event_ids UUID[] NOT NULL,
          confidence REAL NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          version INT NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'needs_review'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_templates (
          id UUID PRIMARY KEY,
          name TEXT NOT NULL,
          domain TEXT NOT NULL,
          graph JSONB NOT NULL,
          triggers JSONB NOT NULL,
          version INT NOT NULL DEFAULT 1,
          updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS context_bundles (
          id UUID PRIMARY KEY,
          query_hash TEXT NOT NULL UNIQUE,
          bundle JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          ttl_seconds INT NOT NULL
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
          id UUID PRIMARY KEY,
          ts TIMESTAMPTZ NOT NULL,
          consumer TEXT NOT NULL,
          action TEXT NOT NULL,
          query JSONB NOT NULL,
          result_event_ids UUID[] NOT NULL,
          policy_decisions JSONB NOT NULL,
          latency_ms INT NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log (ts DESC)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS policies (
          id UUID PRIMARY KEY,
          name TEXT NOT NULL UNIQUE,
          rules JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        INSERT INTO policies (id, name, rules, created_at)
        VALUES (
          gen_random_uuid(),
          'user-only',
          jsonb_build_object(
            'allowed_domains',
            jsonb_build_array('*'),
            'max_sensitivity',
            2
          ),
          now()
        )
        ON CONFLICT (name) DO NOTHING
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pattern_feedback (
          id UUID PRIMARY KEY,
          pattern_id UUID NOT NULL REFERENCES patterns(id) ON DELETE CASCADE,
          approved BOOLEAN NOT NULL,
          note TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_settings (
          key TEXT PRIMARY KEY,
          value JSONB NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        INSERT INTO runtime_settings (key, value, updated_at)
        VALUES (
          'runtime_mode',
          jsonb_build_object('mode', 'timeline_only', 'clone_enabled', false),
          now()
        )
        ON CONFLICT (key) DO NOTHING
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_interactions (
          id UUID PRIMARY KEY,
          ts TIMESTAMPTZ NOT NULL,
          interaction_id TEXT NOT NULL,
          source_consumer TEXT NOT NULL,
          source_role TEXT NOT NULL,
          target_role TEXT NOT NULL,
          action TEXT NOT NULL,
          citations UUID[] NOT NULL,
          payload JSONB NOT NULL,
          allowed BOOLEAN NOT NULL,
          reason TEXT NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_agent_interactions_ts ON agent_interactions (ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_agent_interactions_interaction_id ON agent_interactions (interaction_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_interactions")
    op.execute("DROP TABLE IF EXISTS runtime_settings")
    op.execute("DROP TABLE IF EXISTS pattern_feedback")
    op.execute("DROP TABLE IF EXISTS policies")
    op.execute("DROP TABLE IF EXISTS audit_log")
    op.execute("DROP TABLE IF EXISTS context_bundles")
    op.execute("DROP TABLE IF EXISTS workflow_templates")
    op.execute("DROP TABLE IF EXISTS patterns")
    op.execute("DROP TABLE IF EXISTS artifacts")
    op.execute("DROP TABLE IF EXISTS event_embeddings")
    op.execute("DROP TABLE IF EXISTS events CASCADE")
