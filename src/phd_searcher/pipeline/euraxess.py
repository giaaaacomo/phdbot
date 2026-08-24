"""Seed della sorgente EURAXESS: una listing page aggregatore, paginata. Niente LLM qui."""

from __future__ import annotations

from injector import Injector
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.pipeline.progress import Progress

# Bandi per First Stage Researcher (R1) = dottorandi. Facet id verificato sul sito (447 = "First
# Stage Researcher (R1)"): il param del piano "researcher_profile" viene ignorato dal sito
# (risultati identici al non filtrato), quello reale è "job_research_profile".
_R1_URL = "https://euraxess.ec.europa.eu/jobs/search?f%5B0%5D=job_research_profile%3A447"
_ALL_URL = "https://euraxess.ec.europa.eu/jobs/search"


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    progress = progress or Progress()
    await progress.begin(2)
    session_maker = container.get(async_sessionmaker[AsyncSession])
    async with session_maker() as session:
        r1 = (await session.execute(select(ListingPage).where(ListingPage.url == _R1_URL))).scalar_one_or_none()
        for label, url in (("EURAXESS R1", _R1_URL), ("EURAXESS all opportunities", _ALL_URL)):
            await progress.tick(label)
            values: dict[str, object] = {
                "url": url,
                "kind": "aggregator",
                "source": "seed",
                "pagination_param": "page",
            }
            # Le due pagine hanno lo stesso markup: riusa lo schema R1 già validato,
            # evitando una seconda chiamata LLM e mantenendo la modalità tool-based.
            if url == _ALL_URL and r1 is not None and r1.extraction_schema:
                values |= {"extraction_schema": r1.extraction_schema, "schema_status": r1.schema_status}
            stmt = pg_insert(ListingPage).values(**values).on_conflict_do_nothing(index_elements=["url"])
            await session.execute(stmt)
        await session.commit()
    print("euraxess: R1 and all-opportunities listing pages seeded")
    return 2
