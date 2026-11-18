"""blacklist DW delivery: tracked sends, delete tombstones, live hits

Image blacklist → lookia-dw, contract agreed in Frontend#dw-online #4932–#4940
(the plates-service pattern, lookia-online-dw#placas-dw #4888).

ADDITIVE ONLY — nullable columns + two new tables, no rewrite, no backfill:

  * blacklist_image_entries / blacklist_image_references
      dw_published_updated_at  the ``updated_at`` value the DW last received.
                               NULL or != updated_at → pending (the sweep sends it).
      dw_publish_error         last failed send, cleared on success.
  * blacklist_image_embeddings
      dw_published_at / dw_publish_error (embeddings are insert-only).
  * blacklist_image_deletions  one row per hard-deleted entry or reference, so the
                               ``.deleted`` tombstone survives a failed send.
  * blacklist_image_matches    one row per live blacklist hit (inline, reverse search,
                               image-index), published to ``blacklist_image_match:raw``.

Every existing row gets NULL = "not yet sent", so the first sweep after the deploy
re-sends the whole blacklist once (the DW upsert is guarded by ``updated_at``).

Revision ID: d7e3a1c9b5f2
Revises: c5d1e8a2f6b9
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d7e3a1c9b5f2"
down_revision = "c5d1e8a2f6b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("blacklist_image_entries", "blacklist_image_references"):
        op.add_column(table, sa.Column("dw_published_updated_at", sa.DateTime(), nullable=True))
        op.add_column(table, sa.Column("dw_publish_error", sa.Text(), nullable=True))

    op.add_column(
        "blacklist_image_embeddings", sa.Column("dw_published_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "blacklist_image_embeddings", sa.Column("dw_publish_error", sa.Text(), nullable=True)
    )

    op.create_table(
        "blacklist_image_deletions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # "entry" | "reference"
        sa.Column("kind", sa.String(length=16), nullable=False),
        # The id of the deleted entry / reference (the DW row_id).
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Set for kind=reference (the parent entry).
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", sa.String(length=255), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=False),
        sa.Column("dw_published_at", sa.DateTime(), nullable=True),
        sa.Column("dw_publish_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("kind", "target_id", name="uq_blacklist_image_deletion_target"),
    )
    op.create_index(
        "ix_blacklist_image_deletions_dw_published_at",
        "blacklist_image_deletions",
        ["dw_published_at"],
    )

    op.create_table(
        "blacklist_image_matches",
        # Deterministic: uuid5(reference_id, matched point, match_target) — the same
        # hit found twice (inline + reverse search, a backfill) is ONE row.
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(length=255), nullable=True),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_id", sa.String(length=255), nullable=True),
        sa.Column("matched_at", sa.DateTime(), nullable=False),
        # The full DW payload (blacklist_image_match.detected), built once at detection.
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dw_published_at", sa.DateTime(), nullable=True),
        sa.Column("dw_publish_error", sa.Text(), nullable=True),
    )
    op.create_index("ix_blacklist_image_matches_entry_id", "blacklist_image_matches", ["entry_id"])
    op.create_index(
        "ix_blacklist_image_matches_dw_published_at", "blacklist_image_matches", ["dw_published_at"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_blacklist_image_matches_dw_published_at", table_name="blacklist_image_matches"
    )
    op.drop_index("ix_blacklist_image_matches_entry_id", table_name="blacklist_image_matches")
    op.drop_table("blacklist_image_matches")
    op.drop_index(
        "ix_blacklist_image_deletions_dw_published_at", table_name="blacklist_image_deletions"
    )
    op.drop_table("blacklist_image_deletions")
    op.drop_column("blacklist_image_embeddings", "dw_publish_error")
    op.drop_column("blacklist_image_embeddings", "dw_published_at")
    for table in ("blacklist_image_references", "blacklist_image_entries"):
        op.drop_column(table, "dw_publish_error")
        op.drop_column(table, "dw_published_updated_at")
