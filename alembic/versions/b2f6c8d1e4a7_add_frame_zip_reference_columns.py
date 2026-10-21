"""add frame-from-ZIP reference columns

Frames desde ZIP (Lookia Platform#frames-desde-zip): in FRAME_STORAGE_MODE=zip_ref
the ingest no longer uploads frames; each vector keeps a reference to its frame
inside the evidence ZIP, and GET /api/v1/frames/{qdrant_point_id} serves it.

Additive ONLY — every new column is nullable, no existing column is touched:
  * evidence_embeddings: zip_url, image_name, zip_entry (raw path inside the ZIP)
  * embedding_requests:  image_count (len of embeddings_data)
  * search_matches:      qdrant_point_id, image_name, zip_entry (for the
                         image_search.matched event)

The frame endpoint looks rows up by evidence_embeddings.qdrant_point_id. The
initial migration (5317a69cb648) already created ix_evidence_embeddings_qdrant_point_id;
``CREATE INDEX IF NOT EXISTS`` only covers a database where it went missing, and
the downgrade leaves it alone (it belongs to the initial migration).

Revision ID: b2f6c8d1e4a7
Revises: a9c1e4f7b2d8
Create Date: 2026-10-21
"""

import sqlalchemy as sa

from alembic import op

revision = "b2f6c8d1e4a7"
down_revision = "a9c1e4f7b2d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── evidence_embeddings: frame reference per vector ──
    op.add_column("evidence_embeddings", sa.Column("zip_url", sa.Text(), nullable=True))
    op.add_column("evidence_embeddings", sa.Column("image_name", sa.Text(), nullable=True))
    op.add_column("evidence_embeddings", sa.Column("zip_entry", sa.Text(), nullable=True))
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_evidence_embeddings_qdrant_point_id "
        "ON evidence_embeddings (qdrant_point_id)"
    )

    # ── embedding_requests: number of embeddings stored ──
    op.add_column("embedding_requests", sa.Column("image_count", sa.Integer(), nullable=True))

    # ── search_matches: matched vector + its frame reference ──
    op.add_column("search_matches", sa.Column("qdrant_point_id", sa.String(255), nullable=True))
    op.add_column("search_matches", sa.Column("image_name", sa.Text(), nullable=True))
    op.add_column("search_matches", sa.Column("zip_entry", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("search_matches", "zip_entry")
    op.drop_column("search_matches", "image_name")
    op.drop_column("search_matches", "qdrant_point_id")
    op.drop_column("embedding_requests", "image_count")
    op.drop_column("evidence_embeddings", "zip_entry")
    op.drop_column("evidence_embeddings", "image_name")
    op.drop_column("evidence_embeddings", "zip_url")
