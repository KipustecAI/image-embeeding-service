"""add helmet analysis fields to embedding_requests

Helmet enrichment of embeddings:results (docs/helmets/CONTRACT.md).
Additive ONLY — every column is nullable, no default and no index, so the
upgrade does not rewrite or scan the table. Rows ingested before this
migration keep helmet_analyzed = NULL.

Revision ID: c5e9a2d7f1b3
Revises: d7e3a1c9b5f2
Create Date: 2026-11-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c5e9a2d7f1b3"
down_revision = "d7e3a1c9b5f2"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("helmet_analyzed", sa.Boolean()),
    ("helmet_verdict", sa.Text()),
    ("helmet_alarm", sa.Boolean()),
    ("helmet_frames_sin_casco", sa.Integer()),
    ("helmet_max_sin_casco", sa.Float()),
    ("helmet_analysis", postgresql.JSONB()),
    ("helmet_analysis_error", sa.Text()),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("embedding_requests", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("embedding_requests", name)
