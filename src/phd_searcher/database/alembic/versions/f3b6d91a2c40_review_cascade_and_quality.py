"""review cascade audit and source quality

Revision ID: f3b6d91a2c40
Revises: ea42b7c91d03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3b6d91a2c40"
down_revision: str | None = "ea42b7c91d03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("review_state", sa.String(length=32), nullable=False, server_default="untriaged"),
    )
    op.add_column("positions", sa.Column("routing_reason", sa.String(length=256), nullable=True))
    op.create_index("ix_positions_review_state", "positions", ["review_state"])
    op.execute(
        """
        UPDATE positions
        SET review_state = CASE
            WHEN screening_status IN ('eligible', 'rejected') THEN 'resolved'
            WHEN screening_status = 'review'
                 AND COALESCE(length(full_description), length(description), 0) < 200
                THEN 'needs_evidence'
            WHEN screening_status = 'review' THEN 'semantic_uncertain'
            ELSE 'untriaged'
        END
        """
    )

    op.add_column(
        "listing_pages",
        sa.Column("quality_status", sa.String(length=16), nullable=False, server_default="unknown"),
    )
    op.add_column("listing_pages", sa.Column("quality_reason", sa.String(length=256), nullable=True))
    op.add_column(
        "listing_pages",
        sa.Column(
            "quality_metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column("listing_pages", sa.Column("quality_checked_at", sa.DateTime(), nullable=True))
    op.create_index("ix_listing_pages_quality_status", "listing_pages", ["quality_status"])

    op.create_table(
        "review_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("pipeline_run_id", sa.Integer(), nullable=True),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("raw_decision", sa.String(length=16), nullable=False),
        sa.Column("accepted_status", sa.String(length=16), nullable=False),
        sa.Column("position_type", sa.String(length=32), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("tool_attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("latency_seconds", sa.Float(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_attempts_position_id", "review_attempts", ["position_id"])
    op.create_index("ix_review_attempts_pipeline_run_id", "review_attempts", ["pipeline_run_id"])
    op.create_index("ix_review_attempts_stage", "review_attempts", ["stage"])
    op.create_index("ix_review_attempts_accepted_status", "review_attempts", ["accepted_status"])
    # Trasforma il verdetto corrente delle versioni precedenti nel primo evento
    # dello storico. Non inventa una pipeline_run_id che non era stata salvata.
    op.execute(
        """
        INSERT INTO review_attempts (
            position_id, pipeline_run_id, stage, model, version, raw_decision,
            accepted_status, position_type, confidence, evidence, reason,
            tool_attempts, details
        )
        SELECT
            id,
            NULL,
            CASE WHEN screening_manual THEN 'manual' ELSE 'legacy' END,
            screening_model,
            COALESCE(screening_version, 'pre-audit'),
            COALESCE(screening_decision, screening_status),
            screening_status,
            position_type,
            screening_confidence,
            CASE
                WHEN screening_evidence IS NULL OR btrim(screening_evidence) = '' THEN '[]'::jsonb
                WHEN pg_input_is_valid(screening_evidence, 'jsonb')
                    THEN CASE
                        WHEN jsonb_typeof(screening_evidence::jsonb) = 'array'
                            THEN screening_evidence::jsonb
                        ELSE jsonb_build_array(screening_evidence::jsonb)
                    END
                ELSE jsonb_build_array(screening_evidence)
            END,
            screening_reason,
            1,
            '{}'::jsonb
        FROM positions
        WHERE screening_status IN ('eligible', 'review', 'rejected')
          AND COALESCE(screening_decision, screening_status) IN ('eligible', 'review', 'rejected')
        """
    )


def downgrade() -> None:
    op.drop_index("ix_review_attempts_accepted_status", table_name="review_attempts")
    op.drop_index("ix_review_attempts_stage", table_name="review_attempts")
    op.drop_index("ix_review_attempts_pipeline_run_id", table_name="review_attempts")
    op.drop_index("ix_review_attempts_position_id", table_name="review_attempts")
    op.drop_table("review_attempts")

    op.drop_index("ix_listing_pages_quality_status", table_name="listing_pages")
    op.drop_column("listing_pages", "quality_checked_at")
    op.drop_column("listing_pages", "quality_metrics")
    op.drop_column("listing_pages", "quality_reason")
    op.drop_column("listing_pages", "quality_status")

    op.drop_index("ix_positions_review_state", table_name="positions")
    op.drop_column("positions", "routing_reason")
    op.drop_column("positions", "review_state")
