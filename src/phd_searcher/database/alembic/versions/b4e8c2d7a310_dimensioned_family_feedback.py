"""store dimensioned and versioned source-family feedback

Revision ID: b4e8c2d7a310
Revises: a9c4e7f1b620
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4e8c2d7a310"
down_revision: str | None = "a9c4e7f1b620"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_position_feedback_reason", "position_feedback", type_="check")
    op.add_column(
        "position_feedback",
        sa.Column("dimension", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "position_feedback",
        sa.Column("value", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "position_feedback",
        sa.Column("source_family_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "position_feedback",
        sa.Column("source_family_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "position_feedback",
        sa.Column("context_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_position_feedback_reason",
        "position_feedback",
        "reason IN ('confirmed_opportunity', 'non_opportunity', 'closed', 'duplicate', "
        "'wrong_type', 'mismatched_details', 'broken_link', 'other')",
    )
    op.create_index(
        "ix_position_feedback_dimension",
        "position_feedback",
        ["dimension"],
    )
    op.create_index(
        "uq_position_feedback_open_dimension",
        "position_feedback",
        ["position_id", "dimension"],
        unique=True,
        postgresql_where=sa.text("status = 'open' AND dimension IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_position_feedback_open_dimension", table_name="position_feedback")
    op.drop_index("ix_position_feedback_dimension", table_name="position_feedback")
    op.drop_constraint("ck_position_feedback_reason", "position_feedback", type_="check")
    op.create_check_constraint(
        "ck_position_feedback_reason",
        "position_feedback",
        "reason IN ('non_opportunity', 'closed', 'duplicate', 'wrong_type', "
        "'mismatched_details', 'broken_link', 'other')",
    )
    op.drop_column("position_feedback", "context_snapshot")
    op.drop_column("position_feedback", "source_family_keys")
    op.drop_column("position_feedback", "source_family_version")
    op.drop_column("position_feedback", "value")
    op.drop_column("position_feedback", "dimension")
