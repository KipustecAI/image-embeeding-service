"""add qdrant_query_point_id to search_requests

Revision ID: c8030dc66743
Revises: 75010ceac028
Create Date: 2026-03-27 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c8030dc66743'
down_revision: Union[str, Sequence[str], None] = '75010ceac028'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'search_requests',
        sa.Column('qdrant_query_point_id', sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('search_requests', 'qdrant_query_point_id')
