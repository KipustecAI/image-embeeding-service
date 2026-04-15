"""Add weapon_analysis_error column to embedding_requests

Captures the failure reason when the compute-weapons producer attempts
analysis and fails. Distinguishes "weapons attempted and failed" from
"weapons never attempted" (legacy / routing skipped), which used to be
collapsed into the same (weapon_analyzed=false, has_weapon=false) state.

See docs/weapons/CONTRACT.md §5 and the image-weapons-compute producer
contract §3.2 (failure pass-through).

Revision ID: c8e5a7b2d4f9
Revises: b7d4f9a3e2c1
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa

revision = "c8e5a7b2d4f9"
down_revision = "b7d4f9a3e2c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "embedding_requests",
        sa.Column("weapon_analysis_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("embedding_requests", "weapon_analysis_error")
