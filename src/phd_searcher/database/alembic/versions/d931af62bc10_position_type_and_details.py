"""position type and full details

Revision ID: d931af62bc10
Revises: c84d23a91ef4
Create Date: 2026-07-21 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d931af62bc10"
down_revision: str | None = "c84d23a91ef4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("position_type", sa.String(length=32), nullable=False, server_default="other"),
    )
    op.add_column("positions", sa.Column("full_description", sa.Text(), nullable=True))
    op.add_column("positions", sa.Column("details_scraped_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "details_scraped_at")
    op.drop_column("positions", "full_description")
    op.drop_column("positions", "position_type")
