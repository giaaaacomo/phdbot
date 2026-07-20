"""pipeline runs

Revision ID: 9c4f1e7a2d3b
Revises: 5095eb3cc00e
Create Date: 2026-07-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9c4f1e7a2d3b"
down_revision: str | None = "5095eb3cc00e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("stages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stages_done", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("current_stage", sa.String(length=32), nullable=True),
        sa.Column("stage_total", sa.Integer(), nullable=True),
        sa.Column("stage_done", sa.Integer(), nullable=False),
        sa.Column("current_label", sa.String(length=512), nullable=True),
        sa.Column("stage_elapsed_seconds", sa.Float(), nullable=False),
        sa.Column("started_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("pipeline_runs")
