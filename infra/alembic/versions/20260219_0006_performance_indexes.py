"""performance indexes: trigram for ILIKE, entity_id for JOIN, title gin"""

from __future__ import annotations

from alembic import op

revision = "20260219_0006"
down_revision = "20260219_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pg_trgm extension for trigram indexes (accelerates ILIKE queries)
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # events is partitioned; parent index creation cannot be concurrent.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_title_trgm "
        "ON events USING GIN (title gin_trgm_ops)"
    )

    # CREATE INDEX CONCURRENTLY requires autocommit (outside migration tx block).
    with op.get_context().autocommit_block():
        # Trigram GIN index on entity_nodes entity_key and display_name — accelerates entity graph boost
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_entity_nodes_key_trgm "
            "ON entity_nodes USING GIN (entity_key gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_entity_nodes_name_trgm "
            "ON entity_nodes USING GIN (display_name gin_trgm_ops)"
        )

        # Index on event_entity_links.entity_id — the search JOIN goes entity_nodes → event_entity_links
        # Current index is (workspace_id, event_id) but the JOIN needs entity_id
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_event_entity_links_entity "
            "ON event_entity_links (entity_id)"
        )

        # Covering index on event_embeddings for IN-list lookups
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_event_embeddings_event_id "
            "ON event_embeddings (event_id)"
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_event_embeddings_event_id")
    op.execute("DROP INDEX IF EXISTS idx_event_entity_links_entity")
    op.execute("DROP INDEX IF EXISTS idx_entity_nodes_name_trgm")
    op.execute("DROP INDEX IF EXISTS idx_entity_nodes_key_trgm")
    op.execute("DROP INDEX IF EXISTS idx_events_title_trgm")
