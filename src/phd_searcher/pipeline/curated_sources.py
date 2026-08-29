"""Small, audited registry for official portals missed by generic discovery."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
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

_CURATED_BY_WIKIDATA: dict[str, tuple[tuple[str, dict[str, object]], ...]] = {
    "Q11942": ((ETH_ZURICH_JOBS_URL, ETH_ZURICH_JOBS_SCHEMA),),
}


async def seed_curated_sources(session: AsyncSession, university: University) -> int:
    """Upsert official sources for one institution, preserving normal discovery."""

    sources = _CURATED_BY_WIKIDATA.get(university.wikidata_id, ())
    for url, schema in sources:
        statement = (
            pg_insert(ListingPage)
            .values(
                university_id=university.id,
                url=url,
                kind="university",
                source="seed",
                extraction_schema=schema,
                schema_status="ok",
            )
            .on_conflict_do_update(
                index_elements=["url"],
                set_={
                    "university_id": university.id,
                    "kind": "university",
                    "source": "seed",
                    "extraction_schema": schema,
                    "schema_status": "ok",
                },
            )
        )
        await session.execute(statement)
    return len(sources)
