"""Create blacklist_image_entries / references / embeddings tables.

Three-table spine for the image blacklist feature. Mirrors the face-blacklist
pattern from deepface-restapi but scoped to CLIP image embeddings.

See docs/image-blacklist/02_DATABASE.md for the schema rationale.

Revision ID: e7f2c9a1b3d6
Revises: d4a9b7c3e5f8
Create Date: 2026-04-18
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e7f2c9a1b3d6"
down_revision = "d4a9b7c3e5f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── blacklist_image_entries — the profile ──
    op.create_table(
        "blacklist_image_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "is_archived", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column(
            "blacklist_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("match_threshold", sa.Float(), nullable=True),
        sa.Column("json_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_blacklist_image_entries_name", "blacklist_image_entries", ["name"]
    )
    op.create_index(
        "ix_blacklist_image_entries_category", "blacklist_image_entries", ["category"]
    )
    op.create_index(
        "ix_blacklist_image_entries_status", "blacklist_image_entries", ["status"]
    )
    op.create_index(
        "ix_blacklist_image_entries_active", "blacklist_image_entries", ["active"]
    )
    op.create_index(
        "ix_blacklist_image_entries_user_id", "blacklist_image_entries", ["user_id"]
    )
    op.create_index(
        "ix_blacklist_image_entries_created_at",
        "blacklist_image_entries",
        ["created_at"],
    )

    # ── blacklist_image_references — reference photos ──
    op.create_table(
        "blacklist_image_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("blacklist_image_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("image_url", sa.Text(), nullable=False),
        sa.Column("image_type", sa.String(50), server_default="reference"),
        sa.Column("status", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("json_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("entry_id", "image_url", name="uq_entry_image"),
    )
    op.create_index(
        "ix_blacklist_image_references_entry_id",
        "blacklist_image_references",
        ["entry_id"],
    )

    # ── blacklist_image_embeddings — Qdrant point references ──
    op.create_table(
        "blacklist_image_embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("blacklist_image_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reference_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("blacklist_image_references.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("qdrant_point_id", sa.String(255), nullable=False, unique=True),
        sa.Column("model_version", sa.String(50), nullable=False),
        sa.Column("json_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_blacklist_image_embeddings_entry_id",
        "blacklist_image_embeddings",
        ["entry_id"],
    )
    op.create_index(
        "ix_blacklist_image_embeddings_reference_id",
        "blacklist_image_embeddings",
        ["reference_id"],
    )
    op.create_index(
        "ix_blacklist_image_embeddings_created_at",
        "blacklist_image_embeddings",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("blacklist_image_embeddings")
    op.drop_table("blacklist_image_references")
    op.drop_table("blacklist_image_entries")
