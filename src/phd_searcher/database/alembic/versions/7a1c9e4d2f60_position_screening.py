"""reversible position screening

Revision ID: 7a1c9e4d2f60
Revises: a81bc20e490f
Create Date: 2026-07-28 03:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a1c9e4d2f60"
down_revision: str | None = "a81bc20e490f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("screening_status", sa.String(length=16), nullable=False, server_default="pending"),
    )
    op.add_column("positions", sa.Column("screening_reason", sa.String(length=256), nullable=True))
    op.add_column(
        "positions",
        sa.Column("screening_manual", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("positions", sa.Column("screened_at", sa.DateTime(), nullable=True))
    op.create_index("ix_positions_screening_status", "positions", ["screening_status"])
    op.execute(
        """
        UPDATE positions
        SET screening_status = CASE
                WHEN position_type <> 'other' THEN 'eligible'
                WHEN full_description IS NOT NULL THEN 'review'
                ELSE 'pending'
            END,
            screening_reason = CASE
                WHEN position_type <> 'other' THEN 'recognized_type:' || position_type
                WHEN full_description IS NOT NULL THEN 'unclassified_after_detail'
                ELSE NULL
            END,
            screened_at = CASE
                WHEN position_type <> 'other' OR full_description IS NOT NULL THEN CURRENT_TIMESTAMP
                ELSE NULL
            END
        """
    )


def downgrade() -> None:
    op.drop_index("ix_positions_screening_status", table_name="positions")
    op.drop_column("positions", "screened_at")
    op.drop_column("positions", "screening_manual")
    op.drop_column("positions", "screening_reason")
    op.drop_column("positions", "screening_status")
