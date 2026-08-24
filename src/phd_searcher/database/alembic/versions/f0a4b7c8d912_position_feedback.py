"""auditable reversible position feedback

Revision ID: f0a4b7c8d912
Revises: d8f1a2b3c4d5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f0a4b7c8d912"
down_revision: str | None = "d8f1a2b3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "position_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("retracted_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "reason IN ('non_opportunity', 'closed', 'duplicate', 'wrong_type', "
            "'mismatched_details', 'broken_link', 'other')",
            name="ck_position_feedback_reason",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'retracted')",
            name="ck_position_feedback_status",
        ),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_position_feedback_position_id", "position_feedback", ["position_id"])
    op.create_index("ix_position_feedback_reason", "position_feedback", ["reason"])
    op.create_index("ix_position_feedback_status", "position_feedback", ["status"])


def downgrade() -> None:
    op.drop_index("ix_position_feedback_status", table_name="position_feedback")
    op.drop_index("ix_position_feedback_reason", table_name="position_feedback")
    op.drop_index("ix_position_feedback_position_id", table_name="position_feedback")
    op.drop_table("position_feedback")
