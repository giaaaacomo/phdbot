"""automatic review audit and durable macros

Revision ID: 8d2f6b19c430
Revises: 7a1c9e4d2f60
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8d2f6b19c430"
down_revision: str | None = "7a1c9e4d2f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("screening_source", sa.String(length=32), nullable=False, server_default="rules"))
    op.add_column("positions", sa.Column("screening_confidence", sa.Float(), nullable=True))
    op.add_column("positions", sa.Column("screening_evidence", sa.Text(), nullable=True))
    op.add_column("positions", sa.Column("screening_model", sa.String(length=128), nullable=True))
    op.add_column("positions", sa.Column("screening_version", sa.String(length=64), nullable=True))
    # Recupera le internship già raccolte prima dell'introduzione della
    # categoria. Il match resta limitato al titolo per non promuovere pagine
    # il cui menu cita genericamente tirocini.
    op.execute(
        """
        UPDATE positions
        SET position_type = 'internship',
            screening_status = CASE
                WHEN screening_manual IS FALSE AND screening_status IN ('pending', 'review')
                    THEN 'eligible'
                ELSE screening_status
            END,
            screening_reason = CASE
                WHEN screening_manual IS FALSE AND screening_status IN ('pending', 'review')
                    THEN 'recognized_type:internship'
                ELSE screening_reason
            END,
            screening_source = CASE
                WHEN screening_manual IS FALSE AND screening_status IN ('pending', 'review')
                    THEN 'rules'
                ELSE screening_source
            END,
            screening_confidence = CASE
                WHEN screening_manual IS FALSE AND screening_status IN ('pending', 'review')
                    THEN 1.0
                ELSE screening_confidence
            END,
            screening_version = CASE
                WHEN screening_manual IS FALSE AND screening_status IN ('pending', 'review')
                    THEN 'taxonomy:internship-v1'
                ELSE screening_version
            END,
            screened_at = CASE
                WHEN screening_manual IS FALSE AND screening_status IN ('pending', 'review')
                    THEN CURRENT_TIMESTAMP
                ELSE screened_at
            END,
            indexed_at = NULL
        WHERE position_type = 'other'
          AND title ~* (
              '(^|[^[:alpha:]])(intern(ship)?|traineeship|tirocin(io|ante)|'
              'praktik(um|ant(in)?)|stageplaats|onderzoeksstage)([^[:alpha:]]|$)|'
              'offre de stage|stage (de|en|di|in) '
              '(recherche|ricerca|research|laboratoire|laboratory)|'
              '(prácticas?|pasantía) de investigación|'
              'estágio de (investigação|pesquisa)'
          )
        """
    )

    op.create_table(
        "saved_macros",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("refresh", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("pipeline_params", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("search_body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("export_formats", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='["html"]'),
        sa.Column("destination", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "macro_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("macro_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("current_step", sa.String(length=64), nullable=True),
        sa.Column("pipeline_run_id", sa.Integer(), nullable=True),
        sa.Column("outputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["macro_id"], ["saved_macros.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_macro_runs_macro_id", "macro_runs", ["macro_id"])
    op.create_index("ix_macro_runs_state", "macro_runs", ["state"])


def downgrade() -> None:
    op.drop_index("ix_macro_runs_state", table_name="macro_runs")
    op.drop_index("ix_macro_runs_macro_id", table_name="macro_runs")
    op.drop_table("macro_runs")
    op.drop_table("saved_macros")
    op.drop_column("positions", "screening_version")
    op.drop_column("positions", "screening_model")
    op.drop_column("positions", "screening_evidence")
    op.drop_column("positions", "screening_confidence")
    op.drop_column("positions", "screening_source")
