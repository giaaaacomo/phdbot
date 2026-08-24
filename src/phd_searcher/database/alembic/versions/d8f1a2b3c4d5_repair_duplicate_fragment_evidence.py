"""repair duplicate evidence assigned to synthetic URL fragments

Revision ID: d8f1a2b3c4d5
Revises: c2d4e6f8a010
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d8f1a2b3c4d5"
down_revision: str | None = "c2d4e6f8a010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A detail dossier must be attributable to one listing item.  If two
    # distinct fragment URLs under the same base page contain byte-identical
    # fetched text, the crawler stored their shared catalogue rather than an
    # item panel.  Keep a sufficiently rich item snippet when available;
    # otherwise clear the false dossier and let the improved fragment fetcher
    # make one controlled retry during the next evidence run.
    op.execute(
        r"""
        WITH duplicate_documents AS (
            SELECT
                split_part(url, '#', 1) AS base_url,
                md5(full_description) AS document_hash
            FROM positions
            WHERE position('#' in url) > 0
              AND full_description IS NOT NULL
            GROUP BY split_part(url, '#', 1), md5(full_description)
            HAVING count(*) > 1
        ),
        targets AS (
            SELECT
                p.id,
                COALESCE(NULLIF(btrim(p.description), ''), p.title, '') AS inline_text,
                (
                    length(
                        regexp_replace(
                            COALESCE(NULLIF(btrim(p.description), ''), p.title, ''),
                            '\s+',
                            ' ',
                            'g'
                        )
                    ) >= 200
                    AND strpos(
                        lower(
                            regexp_replace(
                                COALESCE(NULLIF(btrim(p.description), ''), p.title, ''),
                                '\s+',
                                ' ',
                                'g'
                            )
                        ),
                        lower(
                            regexp_replace(
                                split_part(COALESCE(p.title, ''), '||', 1),
                                '\s+',
                                ' ',
                                'g'
                            )
                        )
                    ) > 0
                    AND lower(
                        regexp_replace(
                            COALESCE(NULLIF(btrim(p.description), ''), p.title, ''),
                            '\s+',
                            ' ',
                            'g'
                        )
                    ) <> lower(regexp_replace(COALESCE(p.title, ''), '\s+', ' ', 'g'))
                ) AS inline_sufficient
            FROM positions AS p
            JOIN duplicate_documents AS d
              ON split_part(p.url, '#', 1) = d.base_url
             AND md5(p.full_description) = d.document_hash
            WHERE NOT p.screening_manual
        )
        UPDATE positions AS p
        SET
            full_description = CASE
                WHEN t.inline_sufficient THEN t.inline_text
                ELSE NULL
            END,
            compensation_raw = CASE
                WHEN t.inline_sufficient
                  AND (p.compensation_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.compensation_raw)) > 0
                  ) THEN p.compensation_raw
                ELSE NULL
            END,
            compensation_min = CASE
                WHEN t.inline_sufficient
                  AND (p.compensation_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.compensation_raw)) > 0
                  ) THEN p.compensation_min
                ELSE NULL
            END,
            compensation_max = CASE
                WHEN t.inline_sufficient
                  AND (p.compensation_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.compensation_raw)) > 0
                  ) THEN p.compensation_max
                ELSE NULL
            END,
            compensation_currency = CASE
                WHEN t.inline_sufficient
                  AND (p.compensation_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.compensation_raw)) > 0
                  ) THEN p.compensation_currency
                ELSE NULL
            END,
            compensation_period = CASE
                WHEN t.inline_sufficient
                  AND (p.compensation_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.compensation_raw)) > 0
                  ) THEN p.compensation_period
                ELSE NULL
            END,
            duration_raw = CASE
                WHEN t.inline_sufficient
                  AND (p.duration_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.duration_raw)) > 0
                  ) THEN p.duration_raw
                ELSE NULL
            END,
            deadline_raw = CASE
                WHEN t.inline_sufficient
                  AND (p.deadline_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.deadline_raw)) > 0
                  ) THEN p.deadline_raw
                ELSE NULL
            END,
            deadline = CASE
                WHEN t.inline_sufficient
                  AND (p.deadline_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.deadline_raw)) > 0
                  ) THEN p.deadline
                ELSE NULL
            END,
            published_raw = CASE
                WHEN t.inline_sufficient
                  AND (p.published_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.published_raw)) > 0
                  ) THEN p.published_raw
                ELSE NULL
            END,
            published_at = CASE
                WHEN t.inline_sufficient
                  AND (p.published_raw IS NULL
                  OR strpos(lower(t.inline_text), lower(p.published_raw)) > 0
                  ) THEN p.published_at
                ELSE NULL
            END,
            research_group = CASE
                WHEN t.inline_sufficient
                  AND (p.research_group IS NULL
                  OR strpos(lower(t.inline_text), lower(p.research_group)) > 0
                  ) THEN p.research_group
                ELSE NULL
            END,
            screening_status = 'review',
            screening_reason = 'evidence:duplicate_fragment_repaired',
            screening_source = 'rules',
            screening_decision = 'review',
            screening_confidence = NULL,
            screening_evidence = NULL,
            screening_model = NULL,
            screening_version = NULL,
            review_state = CASE
                WHEN t.inline_sufficient THEN 'ready_deep_review'
                ELSE 'needs_evidence'
            END,
            routing_reason = CASE
                WHEN t.inline_sufficient THEN 'evidence:inline_duplicate_repaired'
                ELSE 'evidence:duplicate_fragment_refetch'
            END,
            details_scraped_at = NULL,
            indexed_at = NULL,
            screened_at = now()
        FROM targets AS t
        WHERE p.id = t.id
        """
    )


def downgrade() -> None:
    # The discarded value was duplicate, non-attributable catalogue text.  It
    # cannot be restored safely; the mandatory pre-migration dump is the
    # recovery point.
    pass
