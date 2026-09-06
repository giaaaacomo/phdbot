"""add research institutions and repair audited official job sources

Revision ID: e3b7c1a9d620
Revises: c7e9a4d2f810
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision: str = "e3b7c1a9d620"
down_revision: str | None = "c7e9a4d2f810"
branch_labels: str | None = None
depends_on: str | None = None

_ISTI_QID = "Q3803752"
_ISTI_URL = "https://www.isti.cnr.it/it/"
_ISTI_CALLS_URL = "https://www.isti.cnr.it/it/comunicazioni/bandi"
_ISTI_CALLS_SCHEMA: dict[str, object] = {
    "name": "ISTI-CNR official calls",
    "baseSelector": ".content-category .list-group > a.list-group-item",
    "baseFields": [
        {"name": "url", "type": "attribute", "attribute": "href"},
    ],
    "fields": [
        {"name": "title", "type": "text", "selector": "h5"},
        {"name": "description", "type": "text", "selector": "p"},
        {"name": "published", "type": "text", "selector": "small"},
        {"name": "deadline", "type": "text", "selector": "p"},
        {"name": "area", "type": "text", "selector": "p"},
    ],
}
_FBK_QID = "Q3747148"
_FBK_URL = "https://www.fbk.eu/"
_FBK_JOBS_URL = "https://jobs.fbk.eu/"
_FBK_JOBS_SCHEMA: dict[str, object] = {
    "name": "Fondazione Bruno Kessler official jobs",
    "dateFormats": {"published": "%m/%d/%Y", "deadline": "%m/%d/%Y"},
    "baseSelector": "table.GRID tr[class^='GRID_DAT_ROW']",
    "baseFields": [],
    "fields": [
        {"name": "title", "type": "text", "selector": "td[data-title='Title'] a"},
        {
            "name": "url",
            "type": "attribute",
            "selector": "td[data-title='Title'] a",
            "attribute": "href",
        },
        {
            "name": "description",
            "type": "text",
            "selector": "td[data-title='Recruitment Type']",
        },
        {
            "name": "published",
            "type": "text",
            "selector": "td[data-title='Date published']",
        },
        {
            "name": "deadline",
            "type": "text",
            "selector": "td[data-title='Deadline']",
        },
    ],
}
_IMPERIAL_QID = "Q189022"
_IMPERIAL_JOBS_URL = "https://www.imperial.ac.uk/jobs/search-jobs/"
_IMPERIAL_JOBS_SCHEMA: dict[str, object] = {
    "name": "Imperial College London official TalentLink jobs",
    "adapter": "talentlink",
    "apiHost": "https://emea3.recruitmentplatform.com",
    "siteTechId": "PMMFK026203F3VBQB8NLOV4CQ",
    "language": "en_GB",
    "pageSize": 100,
    "salaryField": "SLOVLIST74",
    "salaryCurrency": "GBP",
    "departmentFields": ["SLOVLIST76", "SLOVLIST72", "SLOVLIST20"],
    "baseSelector": "body",
    "baseFields": [],
    "fields": [{"name": "title", "type": "text", "selector": "h1"}],
}
_TURKU_QID = "Q501841"
_TURKU_JOBS_URL = "https://www.utu.fi/en/university/come-work-with-us/open-vacancies"
_TURKU_JOBS_SCHEMA: dict[str, object] = {
    "name": "University of Turku official TalentAdore jobs",
    "adapter": "talentadore",
    "feedUrl": (
        "https://ats.talentadore.com/positions/3VMfJS4/json"
        "?v=2&display_language=en&tags=&notTags=&businessUnits="
        "&notBusinessUnits=&display_description=job_ad&categories=tags_and_extras"
    ),
    "baseSelector": "body",
    "baseFields": [],
    "fields": [{"name": "title", "type": "text", "selector": "h1"}],
}


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO universities (
                wikidata_id, name, country, website_url, description,
                catalog_tier, catalog_basis, sitelinks, discovery_status
            ) VALUES (
                :qid, :name, 'IT', :website_url, :description,
                'research', :basis, 2, 'done'
            )
            ON CONFLICT (wikidata_id) DO UPDATE SET
                name = EXCLUDED.name,
                country = EXCLUDED.country,
                website_url = EXCLUDED.website_url,
                description = EXCLUDED.description,
                catalog_tier = EXCLUDED.catalog_tier,
                catalog_basis = EXCLUDED.catalog_basis
            """
        ),
        {
            "qid": _ISTI_QID,
            "name": (
                "Institute of Information Science and Technologies "
                '"Alessandro Faedo" (ISTI-CNR)'
            ),
            "website_url": _ISTI_URL,
            "description": (
                "Computer-science research institute of the Italian National "
                "Research Council in Pisa"
            ),
            "basis": "curated:official-site;wikidata:Q3803752",
        },
    )
    for qid, url, schema, pagination_param in (
        (
            _IMPERIAL_QID,
            _IMPERIAL_JOBS_URL,
            _IMPERIAL_JOBS_SCHEMA,
            "adapter_page",
        ),
        (_TURKU_QID, _TURKU_JOBS_URL, _TURKU_JOBS_SCHEMA, None),
    ):
        bind.execute(
            sa.text(
                """
                INSERT INTO listing_pages (
                    university_id, url, kind, source, extraction_schema,
                    schema_status, pagination_param, quality_status,
                    quality_metrics
                )
                SELECT id, :url, 'university', 'seed', CAST(:schema AS jsonb),
                       'ok', :pagination_param, 'unknown', '{}'::jsonb
                FROM universities
                WHERE wikidata_id = :qid
                ON CONFLICT (url) DO UPDATE SET
                    university_id = EXCLUDED.university_id,
                    kind = EXCLUDED.kind,
                    source = EXCLUDED.source,
                    extraction_schema = EXCLUDED.extraction_schema,
                    schema_status = EXCLUDED.schema_status,
                    pagination_param = EXCLUDED.pagination_param,
                    quality_status = 'unknown',
                    quality_reason = NULL,
                    quality_metrics = '{}'::jsonb,
                    quality_checked_at = NULL,
                    last_scraped_at = NULL
                """
            ),
            {
                "qid": qid,
                "url": url,
                "schema": json.dumps(schema),
                "pagination_param": pagination_param,
            },
        )
    # These two Imperial pages are navigation/directories, not job cards. The
    # structured official feed above replaces them without deleting audit data.
    bind.execute(
        sa.text(
            """
            UPDATE listing_pages
            SET schema_status = 'unsupported',
                quality_status = 'quarantine',
                quality_reason = 'audited:official_structured_feed_replaces_directory'
            WHERE url IN (
                'https://www.imperial.ac.uk/jobs/',
                'https://www.imperial.ac.uk/jobs/career-programmes/phd-vacancies/'
            )
            """
        )
    )
    # Rows extracted before this migration are preserved for audit, but they
    # must not keep inflating coverage or survive in the search index now that
    # the authoritative vacancy feed replaces them. ``search-jobs`` is included
    # because its old HTML schema produced navigation cards; the first adapter
    # scrape reactivates/upserts the genuine vacancy URLs.
    bind.execute(
        sa.text(
            """
            UPDATE positions
            SET is_active = FALSE,
                indexed_at = NULL
            WHERE listing_page_id IN (
                SELECT id
                FROM listing_pages
                WHERE url IN (
                    'https://www.imperial.ac.uk/jobs/',
                    'https://www.imperial.ac.uk/jobs/career-programmes/phd-vacancies/',
                    'https://www.imperial.ac.uk/jobs/search-jobs/'
                )
            )
            """
        )
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO listing_pages (
                university_id, url, kind, source, extraction_schema,
                schema_status, quality_status, quality_metrics
            )
            SELECT id, :url, 'university', 'seed', CAST(:schema AS jsonb),
                   'ok', 'unknown', '{}'::jsonb
            FROM universities
            WHERE wikidata_id = :qid
            ON CONFLICT (url) DO UPDATE SET
                university_id = EXCLUDED.university_id,
                kind = EXCLUDED.kind,
                source = EXCLUDED.source,
                extraction_schema = EXCLUDED.extraction_schema,
                schema_status = EXCLUDED.schema_status,
                quality_status = 'unknown',
                quality_reason = NULL,
                quality_metrics = '{}'::jsonb,
                quality_checked_at = NULL,
                last_scraped_at = NULL
            """
        ),
        {
            "qid": _ISTI_QID,
            "url": _ISTI_CALLS_URL,
            "schema": json.dumps(_ISTI_CALLS_SCHEMA),
        },
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO universities (
                wikidata_id, name, country, website_url, description,
                catalog_tier, catalog_basis, sitelinks, discovery_status
            ) VALUES (
                :qid, :name, 'IT', :website_url, :description,
                'research', :basis, 4, 'done'
            )
            ON CONFLICT (wikidata_id) DO UPDATE SET
                name = EXCLUDED.name,
                country = EXCLUDED.country,
                website_url = EXCLUDED.website_url,
                description = EXCLUDED.description,
                catalog_tier = EXCLUDED.catalog_tier,
                catalog_basis = EXCLUDED.catalog_basis
            """
        ),
        {
            "qid": _FBK_QID,
            "name": "Fondazione Bruno Kessler (FBK)",
            "website_url": _FBK_URL,
            "description": (
                "Research foundation in Trento working across science, technology "
                "and the humanities"
            ),
            "basis": "curated:official-site;wikidata:Q3747148",
        },
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO listing_pages (
                university_id, url, kind, source, extraction_schema,
                schema_status, quality_status, quality_metrics
            )
            SELECT id, :url, 'university', 'seed', CAST(:schema AS jsonb),
                   'ok', 'unknown', '{}'::jsonb
            FROM universities
            WHERE wikidata_id = :qid
            ON CONFLICT (url) DO UPDATE SET
                university_id = EXCLUDED.university_id,
                kind = EXCLUDED.kind,
                source = EXCLUDED.source,
                extraction_schema = EXCLUDED.extraction_schema,
                schema_status = EXCLUDED.schema_status,
                quality_status = 'unknown',
                quality_reason = NULL,
                quality_metrics = '{}'::jsonb,
                quality_checked_at = NULL,
                last_scraped_at = NULL
            """
        ),
        {
            "qid": _FBK_QID,
            "url": _FBK_JOBS_URL,
            "schema": json.dumps(_FBK_JOBS_SCHEMA),
        },
    )


def downgrade() -> None:
    # Preserve acquired positions and their source on populated installations.
    pass
