"""advisor profiles and runtime health"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260224_0021"
down_revision = "20260223_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS advisor_profiles (
            profile_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'workspace',
            routing_mode TEXT NOT NULL DEFAULT 'adaptive',
            failure_policy TEXT NOT NULL DEFAULT 'risk_aware_fail_safe',
            active BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS advisor_profile_routes (
            id UUID PRIMARY KEY,
            profile_id TEXT NOT NULL REFERENCES advisor_profiles(profile_id) ON DELETE CASCADE,
            provider_id TEXT NOT NULL,
            model TEXT,
            api_key_ref TEXT,
            base_url TEXT,
            api_version TEXT,
            region_hint TEXT,
            priority INT NOT NULL DEFAULT 0,
            provider_category TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS advisor_runtime_health (
            route_key TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model TEXT,
            success_ewma REAL NOT NULL DEFAULT 0.8,
            latency_ewma_ms REAL NOT NULL DEFAULT 350.0,
            consecutive_failures INT NOT NULL DEFAULT 0,
            circuit_state TEXT NOT NULL DEFAULT 'closed',
            half_open_successes INT NOT NULL DEFAULT 0,
            last_error TEXT,
            open_until TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS advisor_switch_audit (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            from_profile_id TEXT,
            to_profile_id TEXT NOT NULL,
            switched_by TEXT NOT NULL,
            reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_advisor_profiles_scope_active
        ON advisor_profiles (workspace_id, user_id, active, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_advisor_profile_routes_profile_priority
        ON advisor_profile_routes (profile_id, priority ASC, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_advisor_runtime_health_scope_updated
        ON advisor_runtime_health (workspace_id, user_id, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_advisor_switch_audit_scope_created
        ON advisor_switch_audit (workspace_id, user_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_advisor_switch_audit_scope_created")
    op.execute("DROP INDEX IF EXISTS idx_advisor_runtime_health_scope_updated")
    op.execute("DROP INDEX IF EXISTS idx_advisor_profile_routes_profile_priority")
    op.execute("DROP INDEX IF EXISTS idx_advisor_profiles_scope_active")
    op.execute("DROP TABLE IF EXISTS advisor_switch_audit")
    op.execute("DROP TABLE IF EXISTS advisor_runtime_health")
    op.execute("DROP TABLE IF EXISTS advisor_profile_routes")
    op.execute("DROP TABLE IF EXISTS advisor_profiles")
