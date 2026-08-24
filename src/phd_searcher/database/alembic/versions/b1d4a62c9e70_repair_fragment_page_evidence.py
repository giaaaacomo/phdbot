"""repair whole-page evidence assigned to URL fragments

Revision ID: b1d4a62c9e70
Revises: f3b6d91a2c40
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b1d4a62c9e70"
down_revision: str | None = "f3b6d91a2c40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # URL fragments never reach the HTTP server.  Older enrich/evidence runs
    # therefore stored the same base page as the detail of several independent
    # fragment records.  Restrict the repair to base pages represented by at
    # least two distinct fragment URLs: this is a durable, data-derived signal
    # and avoids guessing about isolated historical records.
    op.execute(
        r"""
        WITH fragment_bases AS (
            SELECT split_part(url, '#', 1) AS base_url
            FROM positions
            WHERE position('#' in url) > 0
              AND full_description IS NOT NULL
            GROUP BY split_part(url, '#', 1)
            HAVING count(DISTINCT url) > 1
        ),
        targets AS (
            SELECT
                p.id,
                COALESCE(NULLIF(btrim(p.description), ''), p.title, '') AS inline_text,
                length(
                    regexp_replace(
                        COALESCE(NULLIF(btrim(p.description), ''), p.title, ''),
                        '\s+',
                        ' ',
                        'g'
                    )
                ) >= 200 AS inline_sufficient
            FROM positions AS p
            JOIN fragment_bases AS b
              ON split_part(p.url, '#', 1) = b.base_url
            WHERE p.full_description IS NOT NULL
              AND NOT p.screening_manual
              AND p.full_description IS DISTINCT FROM
                  COALESCE(NULLIF(btrim(p.description), ''), p.title, '')
        )
        UPDATE positions AS p
        SET
            full_description = CASE
                WHEN t.inline_sufficient THEN t.inline_text
                ELSE NULL
            END,
            compensation_raw = CASE
                WHEN p.compensation_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.compensation_raw)) > 0
                    THEN p.compensation_raw
                ELSE NULL
            END,
            compensation_min = CASE
                WHEN p.compensation_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.compensation_raw)) > 0
                    THEN p.compensation_min
                ELSE NULL
            END,
            compensation_max = CASE
                WHEN p.compensation_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.compensation_raw)) > 0
                    THEN p.compensation_max
                ELSE NULL
            END,
            compensation_currency = CASE
                WHEN p.compensation_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.compensation_raw)) > 0
                    THEN p.compensation_currency
                ELSE NULL
            END,
            compensation_period = CASE
                WHEN p.compensation_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.compensation_raw)) > 0
                    THEN p.compensation_period
                ELSE NULL
            END,
            duration_raw = CASE
                WHEN p.duration_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.duration_raw)) > 0
                    THEN p.duration_raw
                ELSE NULL
            END,
            published_raw = CASE
                WHEN p.published_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.published_raw)) > 0
                    THEN p.published_raw
                ELSE NULL
            END,
            published_at = CASE
                WHEN p.published_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.published_raw)) > 0
                    THEN p.published_at
                ELSE NULL
            END,
            research_group = CASE
                WHEN p.research_group IS NULL
                  OR strpos(lower(t.inline_text), lower(p.research_group)) > 0
                    THEN p.research_group
                ELSE NULL
            END,
            screening_status = 'review',
            screening_reason = 'evidence:fragment_page_repaired',
            screening_source = 'rules',
            screening_decision = 'review',
            screening_confidence = NULL,
            screening_evidence = NULL,
            screening_model = NULL,
            screening_version = NULL,
            review_state = CASE
                WHEN t.inline_sufficient THEN 'ready_deep_review'
                ELSE 'fetch_unavailable'
            END,
            routing_reason = CASE
                WHEN t.inline_sufficient THEN 'evidence:inline_fragment_repaired'
                ELSE 'evidence:inline_fragment_too_short'
            END,
            details_scraped_at = NULL,
            indexed_at = NULL,
            screened_at = now()
        FROM targets AS t
        WHERE p.id = t.id
        """
    )


def downgrade() -> None:
    # The discarded value was a non-attributable copy of a shared base page.
    # It cannot be reconstructed safely; the pre-deployment database dump is
    # the reversible recovery point for this data correction.
    pass
