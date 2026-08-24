"""add immutable first-seen provenance to positions

Revision ID: a9c4e7f1b620
Revises: f0a4b7c8d912
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c4e7f1b620"
down_revision: str | None = "f0a4b7c8d912"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Adding the nullable column first avoids pretending that the deployment
    # time was the acquisition time of every historical record.
    op.add_column(
        "positions",
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
    )
    op.alter_column(
        "positions",
        "first_seen_at",
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    op.drop_column("positions", "first_seen_at")
