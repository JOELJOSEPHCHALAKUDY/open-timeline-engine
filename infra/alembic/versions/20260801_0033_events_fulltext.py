"""add generated full-text search data for event retrieval"""

from __future__ import annotations

from alembic import op

revision = "20260801_0033"
down_revision = "20260723_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This rewrites the small partitioned table. Larger deployments should build
    # child indexes concurrently and attach them to an ONLY parent index instead.
    op.execute("SET lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE events ADD COLUMN IF NOT EXISTS search_tsv tsvector
          GENERATED ALWAYS AS (
              setweight(to_tsvector('english', left(coalesce(title, ''), 50000)), 'A')
           || setweight(to_tsvector('english', left(coalesce(summary_l0, ''), 50000)), 'B')
           || setweight(
                jsonb_to_tsvector(
                    'english',
                    coalesce(summary_l1_json, '{}'::jsonb),
                    '["string"]'
                ),
                'B'
              )
           || setweight(
                jsonb_to_tsvector(
                    'english',
                    coalesce(tags, '[]'::jsonb),
                    '["string"]'
                ),
                'C'
              )
          ) STORED
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_search_tsv "
        "ON events USING GIN (search_tsv)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_events_search_tsv")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS search_tsv")
