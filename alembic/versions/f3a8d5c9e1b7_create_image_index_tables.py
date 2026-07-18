"""Create image_index_batches / image_index_results tables.

Two-table spine for the on-demand image-index feature (additive / isolated /
gated-OFF). The flag IMAGE_INDEX_ENABLED gates code paths, never DDL — this
migration is unconditional. See docs/image-index/00_DESIGN.md §3.

Status columns are TEXT + CheckConstraint (not the integer status-machines the
live evidence/search tables use) so the vocabulary lines up 1:1 with the
coordinator lifecycle.

Revision ID: f3a8d5c9e1b7
Revises: e7f2c9a1b3d6
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f3a8d5c9e1b7"
down_revision = "e7f2c9a1b3d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── image_index_batches — the job ──
    op.create_table(
        "image_index_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("client_batch_ref", sa.String(255), nullable=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "submitted_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "embedded_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "filtered_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "failed_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("client_batch_ref", name="uq_image_index_batches_client_batch_ref"),
        sa.CheckConstraint(
            "status IN ('pending','computing','completed','completed_with_errors','error')",
            name="ck_image_index_batches_status",
        ),
    )
    op.create_index(
        "ix_image_index_batches_external_id", "image_index_batches", ["external_id"]
    )
    op.create_index(
        "ix_image_index_batches_user_id", "image_index_batches", ["user_id"]
    )
    op.create_index(
        "ix_image_index_batches_status", "image_index_batches", ["status"]
    )
    op.create_index(
        "ix_image_index_batches_created_at", "image_index_batches", ["created_at"]
    )

    # ── image_index_results — per-item disposition + Qdrant ref ──
    op.create_table(
        "image_index_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "batch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("image_index_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_ref", sa.String(255), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("item_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("qdrant_point_id", sa.String(255), nullable=True),
        sa.Column("duplicate_of_index", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("batch_id", "item_index", name="uq_image_index_result_item"),
        sa.UniqueConstraint("qdrant_point_id", name="uq_image_index_result_point"),
        sa.CheckConstraint(
            "status IN ('embedded','download_failed','decode_failed','filtered','no_result')",
            name="ck_image_index_results_status",
        ),
    )
    op.create_index(
        "ix_image_index_results_batch_id", "image_index_results", ["batch_id"]
    )
    op.create_index(
        "ix_image_index_results_status", "image_index_results", ["status"]
    )
    op.create_index(
        "ix_image_index_results_created_at", "image_index_results", ["created_at"]
    )


def downgrade() -> None:
    op.drop_table("image_index_results")
    op.drop_table("image_index_batches")
