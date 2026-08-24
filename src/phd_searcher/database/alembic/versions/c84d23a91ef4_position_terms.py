"""position duration and compensation

Revision ID: c84d23a91ef4
Revises: b7f4c2d91a60
Create Date: 2026-07-21 17:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c84d23a91ef4"
down_revision: str | None = "b7f4c2d91a60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("duration_raw", sa.String(length=256), nullable=True))
    op.add_column("positions", sa.Column("compensation_raw", sa.String(length=512), nullable=True))
    op.add_column("positions", sa.Column("compensation_min", sa.Float(), nullable=True))
    op.add_column("positions", sa.Column("compensation_max", sa.Float(), nullable=True))
    op.add_column("positions", sa.Column("compensation_currency", sa.String(length=3), nullable=True))
    op.add_column("positions", sa.Column("compensation_period", sa.String(length=16), nullable=True))
    op.add_column("positions", sa.Column("published_raw", sa.String(length=256), nullable=True))
    op.add_column("positions", sa.Column("published_at", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "published_at")
    op.drop_column("positions", "published_raw")
    op.drop_column("positions", "compensation_period")
    op.drop_column("positions", "compensation_currency")
    op.drop_column("positions", "compensation_max")
    op.drop_column("positions", "compensation_min")
    op.drop_column("positions", "compensation_raw")
    op.drop_column("positions", "duration_raw")
