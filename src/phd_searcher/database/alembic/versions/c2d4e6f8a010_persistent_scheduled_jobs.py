"""persistent one-shot pipeline and macro schedules

Revision ID: c2d4e6f8a010
Revises: f7c2a18d9b30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c2d4e6f8a010"
down_revision: str | None = "f7c2a18d9b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="scheduled"),
        sa.Column("run_at", sa.DateTime(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Europe/Rome"),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("macro_id", sa.Integer(), nullable=True),
        sa.Column("pipeline_run_id", sa.Integer(), nullable=True),
        sa.Column("macro_run_id", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("target IN ('pipeline', 'macro')", name="ck_scheduled_jobs_target"),
        sa.CheckConstraint(
            "(target = 'pipeline' AND macro_id IS NULL) OR (target = 'macro' AND macro_id IS NOT NULL)",
            name="ck_scheduled_jobs_target_payload",
        ),
        sa.CheckConstraint(
            "state IN ('scheduled', 'waiting_pipeline', 'starting', 'running', 'done', 'failed', 'cancelled')",
            name="ck_scheduled_jobs_state",
        ),
        sa.ForeignKeyConstraint(["macro_id"], ["saved_macros.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["macro_run_id"], ["macro_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduled_jobs_target", "scheduled_jobs", ["target"])
    op.create_index("ix_scheduled_jobs_state", "scheduled_jobs", ["state"])
    op.create_index("ix_scheduled_jobs_run_at", "scheduled_jobs", ["run_at"])
    op.create_index("ix_scheduled_jobs_macro_id", "scheduled_jobs", ["macro_id"])
    op.create_index("ix_scheduled_jobs_next_attempt_at", "scheduled_jobs", ["next_attempt_at"])
    op.create_index(
        "ix_scheduled_jobs_due",
        "scheduled_jobs",
        ["state", "next_attempt_at", "run_at"],
        postgresql_where=sa.text("state IN ('scheduled', 'waiting_pipeline', 'starting')"),
    )

    op.add_column("pipeline_runs", sa.Column("scheduled_job_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_pipeline_runs_scheduled_job_id",
        "pipeline_runs",
        "scheduled_jobs",
        ["scheduled_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_pipeline_runs_scheduled_job_id",
        "pipeline_runs",
        ["scheduled_job_id"],
        unique=True,
    )

    op.add_column("macro_runs", sa.Column("scheduled_job_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_macro_runs_scheduled_job_id",
        "macro_runs",
        "scheduled_jobs",
        ["scheduled_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_macro_runs_scheduled_job_id", "macro_runs", ["scheduled_job_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_macro_runs_scheduled_job_id", table_name="macro_runs")
    op.drop_constraint("fk_macro_runs_scheduled_job_id", "macro_runs", type_="foreignkey")
    op.drop_column("macro_runs", "scheduled_job_id")

    op.drop_index("ix_pipeline_runs_scheduled_job_id", table_name="pipeline_runs")
    op.drop_constraint("fk_pipeline_runs_scheduled_job_id", "pipeline_runs", type_="foreignkey")
    op.drop_column("pipeline_runs", "scheduled_job_id")

    op.drop_index("ix_scheduled_jobs_due", table_name="scheduled_jobs")
    op.drop_index("ix_scheduled_jobs_next_attempt_at", table_name="scheduled_jobs")
    op.drop_index("ix_scheduled_jobs_macro_id", table_name="scheduled_jobs")
    op.drop_index("ix_scheduled_jobs_run_at", table_name="scheduled_jobs")
    op.drop_index("ix_scheduled_jobs_state", table_name="scheduled_jobs")
    op.drop_index("ix_scheduled_jobs_target", table_name="scheduled_jobs")
    op.drop_table("scheduled_jobs")
