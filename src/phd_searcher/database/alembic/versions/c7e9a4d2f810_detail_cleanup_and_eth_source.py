"""durable detail cleanup requests and ETH central source

Revision ID: c7e9a4d2f810
Revises: b4e8c2d7a310
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision: str = "c7e9a4d2f810"
down_revision: str | None = "b4e8c2d7a310"
branch_labels: str | None = None
depends_on: str | None = None

# Migrations are deliberately self-contained: a future application module may
# change while this historical revision must remain replayable from scratch.
_ETH_ZURICH_JOBS_URL = "https://jobs.ethz.ch/site/index"
_ETH_ZURICH_JOBS_SCHEMA: dict[str, object] = {
    "name": "ETH Zurich official jobs",
    "baseSelector": ".job-ad__item__wrapper",
    "baseFields": [],
    "fields": [
        {"name": "title", "type": "text", "selector": ".job-ad__item__title"},
        {
            "name": "url",
            "type": "attribute",
            "selector": "a.job-ad__item__link",
            "attribute": "href",
        },
        {"name": "description", "type": "text", "selector": ".job-ad__item__company"},
        {"name": "area", "type": "text", "selector": ".job-ad__item__company"},
        {"name": "duration", "type": "text", "selector": ".job-ad__item__details"},
        {"name": "published", "type": "text", "selector": ".job-ad__item__company"},
        {"name": "research_group", "type": "text", "selector": ".job-ad__item__company"},
    ],
}


def upgrade() -> None:
    op.add_column("positions", sa.Column("detail_refresh_requested_at", sa.DateTime(), nullable=True))
    op.add_column("positions", sa.Column("detail_cleanup_version", sa.String(length=32), nullable=True))
    op.create_index(
        "ix_positions_detail_refresh_requested_at",
        "positions",
        ["detail_refresh_requested_at"],
        unique=False,
    )

    # Existing populated installations receive the repair now.  On a fresh
    # installation the normal discovery stage seeds it after Q11942 is loaded.
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO listing_pages (
                university_id, url, kind, source, extraction_schema,
                schema_status, quality_status, quality_metrics
            )
            SELECT id, :url, 'university', 'seed', CAST(:schema AS jsonb),
                   'ok', 'unknown', '{}'::jsonb
            FROM universities
            WHERE wikidata_id = 'Q11942'
            ON CONFLICT (url) DO UPDATE SET
                university_id = EXCLUDED.university_id,
                kind = EXCLUDED.kind,
                source = EXCLUDED.source,
                extraction_schema = EXCLUDED.extraction_schema,
                schema_status = EXCLUDED.schema_status
            """
        ),
        {"url": _ETH_ZURICH_JOBS_URL, "schema": json.dumps(_ETH_ZURICH_JOBS_SCHEMA)},
    )


def downgrade() -> None:
    # Keep the harmless discovered source and any positions acquired from it.
    op.drop_index("ix_positions_detail_refresh_requested_at", table_name="positions")
    op.drop_column("positions", "detail_cleanup_version")
    op.drop_column("positions", "detail_refresh_requested_at")
