"""repair QUB description spillover stored as compensation

Revision ID: e6a4b9c2d170
Revises: c1e7d4a9b250
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e6a4b9c2d170"
down_revision: str | None = "c1e7d4a9b250"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # This exact paragraph describes the School's community and the timing of
    # project adverts. It contains no pay, stipend, amount or benefit and was
    # copied into the compensation field by an extraction schema spillover.
    op.execute(
        r"""
        UPDATE positions
        SET compensation_raw = NULL,
            compensation_min = NULL,
            compensation_max = NULL,
            compensation_currency = NULL,
            compensation_period = NULL,
            indexed_at = NULL
        WHERE compensation_raw ILIKE
            'Our PhD community also organizes numerous social events%'
        """
    )


def downgrade() -> None:
    # The cleared value was demonstrably not compensation. Restoring it would
    # reintroduce corrupt derived data; source descriptions remain available.
    pass
