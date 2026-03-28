"""Add ETL fields to embedding_requests: user_id, device_id, app_id, infraction_code

Revision ID: a1e2f3b4c5d6
Revises: c8030dc66743
Create Date: 2026-03-27
"""

from alembic import op
import sqlalchemy as sa

revision = "a1e2f3b4c5d6"
down_revision = "c8030dc66743"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("embedding_requests", sa.Column("user_id", sa.String(255), nullable=True))
    op.add_column("embedding_requests", sa.Column("device_id", sa.String(255), nullable=True))
    op.add_column("embedding_requests", sa.Column("app_id", sa.Integer(), nullable=True))
    op.add_column("embedding_requests", sa.Column("infraction_code", sa.String(255), nullable=True))

    op.create_index("ix_embedding_requests_user_id", "embedding_requests", ["user_id"])
    op.create_index("ix_embedding_requests_infraction_code", "embedding_requests", ["infraction_code"])


def downgrade() -> None:
    op.drop_index("ix_embedding_requests_infraction_code", table_name="embedding_requests")
    op.drop_index("ix_embedding_requests_user_id", table_name="embedding_requests")

    op.drop_column("embedding_requests", "infraction_code")
    op.drop_column("embedding_requests", "app_id")
    op.drop_column("embedding_requests", "device_id")
    op.drop_column("embedding_requests", "user_id")
