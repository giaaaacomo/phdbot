"""add the persistent position opportunity kind

Revision ID: f7c2a18d9b30
Revises: e6a4b9c2d170
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7c2a18d9b30"
down_revision: str | None = "e6a4b9c2d170"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column(
            "opportunity_kind",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
    )
    # Transitional compatibility backfill: keep already eligible results
    # visible to consumers that select vacancies/programmes. Manual decisions
    # remain authoritative; versioned legacy LLM verdicts are progressively
    # revalidated, while the high-precision rule sweep repairs deterministic
    # false positives. Unknown rule-based rows are deliberately not bulk-
    # demoted because that would trade recall for a migration-time guess.
    # indexed_at stays intact; the full index refresh synchronizes this
    # provisional kind into existing Qdrant payloads without re-embedding.
    op.execute(
        """
        UPDATE positions
        SET opportunity_kind = 'vacancy'
        WHERE screening_status = 'eligible'
        """
    )


def downgrade() -> None:
    op.drop_column("positions", "opportunity_kind")
