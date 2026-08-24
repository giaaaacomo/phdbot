"""repair null application deadlines contaminated by neighbouring start dates

Revision ID: c1e7d4a9b250
Revises: b1d4a62c9e70
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c1e7d4a9b250"
down_revision: str | None = "b1d4a62c9e70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Preserve deadline_raw as source evidence.  Only the derived date is
    # corrupt: the old parser crossed into a neighbouring Start Date field.
    # Clearing indexed_at guarantees that already searchable eligible records
    # receive the repaired payload during the next bounded index stage.
    op.execute(
        r"""
        UPDATE positions
        SET deadline = NULL,
            indexed_at = NULL
        WHERE deadline IS NOT NULL
          AND deadline_raw ~* '\m(application[[:space:]]+deadline|closing[[:space:]]+date|deadline)'
          AND deadline_raw ~* '(none([[:space:]]+specified)?|not[[:space:]]+specified|n[[:space:]]*/?[[:space:]]*a|no[[:space:]]+deadline)'
        """
    )


def downgrade() -> None:
    # The previous value was derived from a different field and cannot be
    # restored safely.  The original source string remains in deadline_raw.
    pass
