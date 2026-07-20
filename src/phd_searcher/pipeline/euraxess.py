"""Seed della sorgente EURAXESS: una listing page aggregatore, paginata. Niente LLM qui."""

from __future__ import annotations

from injector import Injector
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.pipeline.progress import Progress

# Bandi per First Stage Researcher (R1) = dottorandi. Facet id verificato sul sito (447 = "First
# Stage Researcher (R1)"): il param del piano "researcher_profile" viene ignorato dal sito
# (risultati identici al non filtrato), quello reale è "job_research_profile".
_URL = "https://euraxess.ec.europa.eu/jobs/search?f%5B0%5D=job_research_profile%3A447"


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    progress = progress or Progress()
    await progress.begin(1)
    await progress.tick("euraxess seed")
    session_maker = container.get(async_sessionmaker[AsyncSession])
    async with session_maker() as session:
        stmt = (
            pg_insert(ListingPage)
            .values(url=_URL, kind="aggregator", source="seed", pagination_param="page")
            .on_conflict_do_nothing(index_elements=["url"])
        )
        await session.execute(stmt)
        await session.commit()
    print("euraxess: listing page seeded")
    return 1
