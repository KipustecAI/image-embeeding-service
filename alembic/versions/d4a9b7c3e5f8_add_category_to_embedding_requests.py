"""Add category column to embedding_requests

Evidence-level category field, propagated from the ETL producer payload
(optional, nullable) through the ingest pipeline to Qdrant and available
as a search filter. Independently useful for narrowing similarity searches
and serves as the foundation for the image-blacklist feature's category
field (see docs/image-blacklist/01_CATEGORY.md).

Revision ID: d4a9b7c3e5f8
Revises: c8e5a7b2d4f9
Create Date: 2026-04-18
"""

from alembic import op
import sqlalchemy as sa

revision = "d4a9b7c3e5f8"
down_revision = "c8e5a7b2d4f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "embedding_requests",
        sa.Column("category", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_embedding_requests_category",
        "embedding_requests",
        ["category"],
    )


def downgrade() -> None:
    op.drop_index("ix_embedding_requests_category", table_name="embedding_requests")
    op.drop_column("embedding_requests", "category")
