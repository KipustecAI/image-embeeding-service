"""add image-index SEARCH discriminator columns

Capability A (async search-by-image) reuses the live ``search_requests`` /
``search_matches`` tables with a discriminator column (02_SEARCH_DESIGN §4).
Additive ONLY — no drop/alter of any existing evidence column; the entire
write-path non-regression story is ``search_type server_default 'evidence'``,
which backfills every existing row.

Revision ID: a9c1e4f7b2d8
Revises: f3a8d5c9e1b7
Create Date: 2026-07-22
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a9c1e4f7b2d8"
down_revision = "f3a8d5c9e1b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── search_requests: discriminator + query external_ids ──
    op.add_column(
        "search_requests",
        sa.Column(
            "search_type",
            sa.String(32),
            nullable=False,
            server_default="evidence",
        ),
    )
    op.add_column(
        "search_requests",
        sa.Column(
            "external_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_search_requests_search_type",
        "search_requests",
        "search_type IN ('evidence','image_index')",
    )
    op.create_index(
        "ix_search_requests_search_type", "search_requests", ["search_type"]
    )

    # ── search_matches: per-match run tag (queryable / frontend grouping) ──
    op.add_column(
        "search_matches",
        sa.Column("external_id", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_search_matches_external_id", "search_matches", ["external_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_search_matches_external_id", "search_matches")
    op.drop_column("search_matches", "external_id")
    op.drop_index("ix_search_requests_search_type", "search_requests")
    op.drop_constraint("ck_search_requests_search_type", "search_requests")
    op.drop_column("search_requests", "external_ids")
    op.drop_column("search_requests", "search_type")
