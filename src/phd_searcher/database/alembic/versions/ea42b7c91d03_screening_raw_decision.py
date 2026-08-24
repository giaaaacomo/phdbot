"""store the raw automatic screening decision

Revision ID: ea42b7c91d03
Revises: d55e6f7a8b90
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ea42b7c91d03"
down_revision: str | None = "d55e6f7a8b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("screening_decision", sa.String(length=16), nullable=True))
    op.execute(
        """
        UPDATE positions
        SET screening_decision = screening_status
        WHERE screening_source IN ('rules', 'manual')
          AND screening_status IN ('pending', 'eligible', 'review', 'rejected')
        """
    )


def downgrade() -> None:
    op.drop_column("positions", "screening_decision")
