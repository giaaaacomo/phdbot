"""incremental refresh state

Revision ID: f61a9d3c742e
Revises: e42bc671da08
Create Date: 2026-07-21 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f61a9d3c742e"
down_revision: str | None = "e42bc671da08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "positions",
        sa.Column("missing_runs", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("universities", sa.Column("discovery_checked_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("universities", "discovery_checked_at")
    op.drop_column("positions", "missing_runs")
    op.drop_column("positions", "is_active")
