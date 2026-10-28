"""add crop label columns

Crop classification (CLS-06, plan clasificacion-de-recortes): the CLIP worker
may attach a vehicle-type label to each kept crop. We store it per vector.

Additive ONLY — both columns are nullable, no existing column is touched, no
backfill (rows ingested before the classifier keep NULL):
  * evidence_embeddings.labels             JSONB  [{"label", "score"}], highest first
  * evidence_embeddings.classifier_version TEXT   e.g. vehicle_types/v20261027-202226

Revision ID: c5d1e8a2f6b9
Revises: b2f6c8d1e4a7
Create Date: 2026-10-28
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c5d1e8a2f6b9"
down_revision = "b2f6c8d1e4a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evidence_embeddings",
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("evidence_embeddings", sa.Column("classifier_version", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("evidence_embeddings", "classifier_version")
    op.drop_column("evidence_embeddings", "labels")
