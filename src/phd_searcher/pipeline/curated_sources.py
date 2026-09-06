"""Small, audited registry for official portals missed by generic discovery."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.university import University

ETH_ZURICH_JOBS_URL = "https://jobs.ethz.ch/site/index"
ETH_ZURICH_JOBS_SCHEMA: dict[str, object] = {
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

ISTI_CNR_CALLS_URL = "https://www.isti.cnr.it/it/comunicazioni/bandi"
ISTI_CNR_CALLS_SCHEMA: dict[str, object] = {
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

FBK_JOBS_URL = "https://jobs.fbk.eu/"
FBK_JOBS_SCHEMA: dict[str, object] = {
    "name": "Fondazione Bruno Kessler official jobs",
    "dateFormats": {"published": "%m/%d/%Y", "deadline": "%m/%d/%Y"},
    "baseSelector": "table.GRID tr[class^='GRID_DAT_ROW']",
    "baseFields": [],
    "fields": [
        {
            "name": "title",
            "type": "text",
            "selector": "td[data-title='Title'] a",
        },
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

IMPERIAL_JOBS_URL = "https://www.imperial.ac.uk/jobs/search-jobs/"
IMPERIAL_JOBS_SCHEMA: dict[str, object] = {
    "name": "Imperial College London official TalentLink jobs",
    "adapter": "talentlink",
    "apiHost": "https://emea3.recruitmentplatform.com",
    "siteTechId": "PMMFK026203F3VBQB8NLOV4CQ",
    "language": "en_GB",
    "pageSize": 100,
    "salaryField": "SLOVLIST74",
    "salaryCurrency": "GBP",
    "departmentFields": ["SLOVLIST76", "SLOVLIST72", "SLOVLIST20"],
    # Valid fallback schema; structured adapters bypass HTML extraction.
    "baseSelector": "body",
    "baseFields": [],
    "fields": [{"name": "title", "type": "text", "selector": "h1"}],
}

TURKU_JOBS_URL = "https://www.utu.fi/en/university/come-work-with-us/open-vacancies"
TURKU_JOBS_SCHEMA: dict[str, object] = {
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

_CURATED_BY_WIKIDATA: dict[str, tuple[tuple[str, dict[str, object]], ...]] = {
    "Q11942": ((ETH_ZURICH_JOBS_URL, ETH_ZURICH_JOBS_SCHEMA),),
    "Q3803752": ((ISTI_CNR_CALLS_URL, ISTI_CNR_CALLS_SCHEMA),),
    "Q3747148": ((FBK_JOBS_URL, FBK_JOBS_SCHEMA),),
    "Q189022": ((IMPERIAL_JOBS_URL, IMPERIAL_JOBS_SCHEMA),),
    "Q501841": ((TURKU_JOBS_URL, TURKU_JOBS_SCHEMA),),
}


async def seed_curated_sources(session: AsyncSession, university: University) -> int:
    """Upsert official sources for one institution, preserving normal discovery.

    An unchanged source keeps its operational/quality history.  A repaired
    schema is deliberately re-queued from a clean ``unknown`` quality state so
    stale quarantine results cannot hide the fix.
    """

    sources = _CURATED_BY_WIKIDATA.get(university.wikidata_id, ())
    for url, schema in sources:
        pagination_param = "adapter_page" if schema.get("adapter") == "talentlink" else None
        existing = await session.scalar(select(ListingPage).where(ListingPage.url == url))
        if existing is None:
            session.add(
                ListingPage(
                    university_id=university.id,
                    url=url,
                    kind="university",
                    source="seed",
                    extraction_schema=schema,
                    schema_status="ok",
                    pagination_param=pagination_param,
                    quality_status="unknown",
                    quality_metrics={},
                )
            )
            continue

        schema_changed = (
            existing.extraction_schema != schema
            or existing.pagination_param != pagination_param
        )
        existing.university_id = university.id
        existing.kind = "university"
        existing.source = "seed"
        if schema_changed:
            existing.extraction_schema = schema
            existing.schema_status = "ok"
            existing.pagination_param = pagination_param
            existing.quality_status = "unknown"
            existing.quality_reason = None
            existing.quality_metrics = {}
            existing.quality_checked_at = None
            existing.last_scraped_at = None
    return len(sources)
