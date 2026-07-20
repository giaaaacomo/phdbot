"""Stadio 3: genera con l'LLM (una tantum) lo schema CSS di estrazione per listing page."""

from __future__ import annotations

import asyncio

from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig
from crawl4ai import LLMConfig as C4ALLMConfig
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy
from injector import Injector
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.config import Settings
from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.university import University
from phd_searcher.pipeline.progress import Progress

_QUERY_UNIVERSITY = (
    "This page lists open PhD positions / doctoral vacancies. Extract the repeated "
    "vacancy items with fields: title (the position title), url (link to the vacancy "
    "detail page), deadline (application deadline text, if shown), description "
    "(short summary text, if shown), area (research area/field, if shown), "
    "language (language of the announcement, if shown)."
)
_QUERY_AGGREGATOR = _QUERY_UNIVERSITY[:-1] + (
    ", institution (the university/organisation offering the position), country (country of the position, if shown)."
)


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    """Ritorna il numero di schemi passati a `ok` (progresso, non tentativi)."""
    progress = progress or Progress()
    settings = container.get(Settings)
    session_maker = container.get(async_sessionmaker[AsyncSession])
    llm_config = C4ALLMConfig(  # crawl4ai usa litellm internamente: stessi model string / api_base
        provider=settings.llm.model,
        # crawl4ai sostituisce il provider con openai/gpt-4o se api_token è falsy: mai passare stringa vuota
        api_token=settings.llm.api_key or "no-token-needed",
        base_url=settings.llm.api_base,
    )
    crawl_config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, check_robots_txt=True)
    generated = 0

    async with session_maker() as session:
        stmt = (
            select(ListingPage, University)
            .outerjoin(University, ListingPage.university_id == University.id)
            .where(ListingPage.schema_status.in_(("missing", "stale", "failed")))
            .order_by(
                case((ListingPage.kind == "aggregator", 0), else_=1),
                case((ListingPage.schema_status == "missing", 0), else_=1),
                func.coalesce(University.sitelinks, 0).desc(),
            )
        )
        if name_like:
            stmt = stmt.where(University.name.ilike(f"%{name_like}%"))
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await session.execute(stmt)).all()
        await progress.begin(len(rows))

        async with AsyncWebCrawler() as crawler:
            for page, uni in rows:
                if progress.should_stop:
                    break
                await progress.tick(uni.name if uni else page.url)
                try:
                    result = await crawler.arun(page.url, config=crawl_config)
                    if not result.success or not result.html:
                        raise RuntimeError("empty page")
                    query = _QUERY_AGGREGATOR if page.kind == "aggregator" else _QUERY_UNIVERSITY
                    schema = await asyncio.to_thread(  # generate_schema è sincrona
                        JsonCssExtractionStrategy.generate_schema,
                        result.html,
                        query=query,
                        llm_config=llm_config,
                    )
                    page.extraction_schema = schema
                    page.schema_status = "ok"
                    generated += 1
                except Exception as exc:
                    print(f"schema_gen failed for {page.url}: {exc}")
                    await session.rollback()  # la sessione può essere invalida dopo un errore DB
                    page.schema_status = "failed"
                await session.commit()
    print(f"schema_gen: {generated} ok ({len(rows)} attempted)")
    return generated
