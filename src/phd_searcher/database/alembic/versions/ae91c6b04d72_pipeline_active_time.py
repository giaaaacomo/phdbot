"""pipeline active time excluding stopped intervals

Revision ID: ae91c6b04d72
Revises: 8d2f6b19c430
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ae91c6b04d72"
down_revision: str | None = "8d2f6b19c430"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline_runs",
        sa.Column("active_elapsed_seconds", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column("pipeline_runs", sa.Column("active_started_at", sa.DateTime(), nullable=True))
    op.add_column("pipeline_runs", sa.Column("active_heartbeat_at", sa.DateTime(), nullable=True))

    # Per le run già concluse il vecchio schema non conservava gli intervalli di stop:
    # il tempo wall-clock è l'unico dato recuperabile. Le nuove run saranno esatte.
    op.execute(
        """
        UPDATE pipeline_runs
        SET active_elapsed_seconds = GREATEST(
            EXTRACT(EPOCH FROM (COALESCE(finished_at, now()) - started_at)),
            0
        ),
        active_started_at = CASE
            WHEN state IN ('running', 'stopping') THEN now()
            ELSE NULL
        END,
        active_heartbeat_at = CASE
            WHEN state IN ('running', 'stopping') THEN now()
            ELSE NULL
        END
        """
    )


def downgrade() -> None:
    op.drop_column("pipeline_runs", "active_heartbeat_at")
    op.drop_column("pipeline_runs", "active_started_at")
    op.drop_column("pipeline_runs", "active_elapsed_seconds")
