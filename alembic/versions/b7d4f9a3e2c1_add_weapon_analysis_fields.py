"""Add weapon analysis fields to embedding_requests and evidence_embeddings

Revision ID: b7d4f9a3e2c1
Revises: a1e2f3b4c5d6
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b7d4f9a3e2c1"
down_revision = "a1e2f3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # embedding_requests: evidence-level weapon flags + summary
    op.add_column(
        "embedding_requests",
        sa.Column(
            "weapon_analyzed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "embedding_requests",
        sa.Column(
            "has_weapon",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "embedding_requests",
        sa.Column(
            "weapon_classes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "embedding_requests",
        sa.Column("weapon_max_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "embedding_requests",
        sa.Column(
            "weapon_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_embedding_requests_weapon_analyzed",
        "embedding_requests",
        ["weapon_analyzed"],
    )
    op.create_index(
        "ix_embedding_requests_has_weapon",
        "embedding_requests",
        ["has_weapon"],
    )

    # evidence_embeddings: per-image detection detail
    op.add_column(
        "evidence_embeddings",
        sa.Column(
            "weapon_detections",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("evidence_embeddings", "weapon_detections")

    op.drop_index("ix_embedding_requests_has_weapon", table_name="embedding_requests")
    op.drop_index("ix_embedding_requests_weapon_analyzed", table_name="embedding_requests")

    op.drop_column("embedding_requests", "weapon_summary")
    op.drop_column("embedding_requests", "weapon_max_confidence")
    op.drop_column("embedding_requests", "weapon_classes")
    op.drop_column("embedding_requests", "has_weapon")
    op.drop_column("embedding_requests", "weapon_analyzed")
