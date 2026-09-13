"""Allow genuine ROR-only institutions without fabricated Wikidata IDs.

Revision ID: a24c9e5b710d
Revises: e3b7c1a9d620
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a24c9e5b710d"
down_revision = "e3b7c1a9d620"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("universities", "wikidata_id", existing_type=sa.String(32), nullable=True)
    op.add_column("universities", sa.Column("ror_id", sa.String(64), nullable=True))
    op.add_column("universities", sa.Column("registry_metadata", postgresql.JSONB(), nullable=True))
    op.create_unique_constraint("uq_universities_ror_id", "universities", ["ror_id"])


def downgrade() -> None:
    # Never silently erase identity/provenance or delete ROR-only institutions.
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM universities WHERE ror_id IS NOT NULL OR wikidata_id IS NULL)")
    ):
        raise RuntimeError("ROR institutions exist; preserve/export their identity before downgrade")
    op.drop_constraint("uq_universities_ror_id", "universities", type_="unique")
    op.drop_column("universities", "registry_metadata")
    op.drop_column("universities", "ror_id")
    op.alter_column("universities", "wikidata_id", existing_type=sa.String(32), nullable=False)
