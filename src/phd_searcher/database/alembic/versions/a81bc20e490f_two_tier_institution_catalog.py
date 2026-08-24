"""two-tier institution catalog

Revision ID: a81bc20e490f
Revises: f61a9d3c742e
Create Date: 2026-07-22 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a81bc20e490f"
down_revision: str | None = "f61a9d3c742e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "universities",
        sa.Column("catalog_tier", sa.String(length=16), nullable=False, server_default="core"),
    )
    op.add_column(
        "universities",
        sa.Column("catalog_basis", sa.String(length=128), nullable=False, server_default="wikidata:Q3918"),
    )
    # Compatibilita con il seed ECAL provvisorio usato prima che fosse trovato il QID.
    op.execute(
        """
        UPDATE universities
        SET wikidata_id = 'Q3577724',
            catalog_tier = 'specialist',
            catalog_basis = 'curated:official-site;wikidata:Q3577724'
        WHERE wikidata_id = 'CURATED_ECAL'
          AND NOT EXISTS (SELECT 1 FROM universities WHERE wikidata_id = 'Q3577724')
        """
    )


def downgrade() -> None:
    op.drop_column("universities", "catalog_basis")
    op.drop_column("universities", "catalog_tier")
