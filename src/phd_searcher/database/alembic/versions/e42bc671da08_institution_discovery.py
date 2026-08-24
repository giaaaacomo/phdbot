"""institution descriptions and research groups

Revision ID: e42bc671da08
Revises: d931af62bc10
Create Date: 2026-07-21 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e42bc671da08"
down_revision: str | None = "d931af62bc10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("universities", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("universities", sa.Column("spontaneous_application_url", sa.String(length=2048), nullable=True))
    op.add_column("positions", sa.Column("research_group", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "research_group")
    op.drop_column("universities", "spontaneous_application_url")
    op.drop_column("universities", "description")
