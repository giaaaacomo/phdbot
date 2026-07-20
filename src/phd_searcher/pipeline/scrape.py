"""Stadio 4: esegue gli schemi cachati (niente LLM), upsert delle Position."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy
from injector import Injector
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.pipeline.normalize import NormalizedPosition, normalize_item
from phd_searcher.pipeline.progress import Progress

_MAX_PAGES = 100  # cap per le listing paginate


async def _fetch_items(
    crawler: AsyncWebCrawler, page: ListingPage, config: CrawlerRunConfig
) -> list[dict[str, object]] | None:
    """Item grezzi della listing (tutte le pagine se paginata). None = fetch fallito."""
    sep = "&" if "?" in page.url else "?"
    items: list[dict[str, object]] = []
    for n in range(_MAX_PAGES if page.pagination_param else 1):
        url = f"{page.url}{sep}{page.pagination_param}={n}" if page.pagination_param else page.url
        result = await crawler.arun(url, config=config)
        if not result.success:
            return None if n == 0 else items
        batch = json.loads(result.extracted_content or "[]")
        if not batch:
            break
        items += batch
    return items


async def _resolve_university(session: AsyncSession, name: str) -> int | None:
    escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")  # input scrapato: escape wildcard
    row = await session.execute(
        select(University.id)
        .where(University.name.ilike(f"%{escaped}%", escape="\\"))
        .order_by(func.length(University.name))  # match più stretto, deterministico
        .limit(1)
    )
    return row.scalar_one_or_none()


def _country_code(raw: object) -> str | None:
    """Solo codici alpha-2 validi: gli aggregatori scrivono nomi per esteso ('Germany' NON è 'GE')."""
    value = str(raw or "").strip().upper()
    return value if len(value) == 2 and value.isalpha() else None


def _position_values(n: NormalizedPosition, page: ListingPage) -> dict[str, object | None]:
    return {
        "url": n.url[:2048],
        "title": n.title[:1024],
        "description": n.description,
        "area": n.area,
        "language": n.language,
        "deadline_raw": n.deadline_raw,
        "deadline": n.deadline,
        "listing_page_id": page.id,
    }


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    """Ritorna il numero di posizioni upsertate."""
    progress = progress or Progress()
    session_maker = container.get(async_sessionmaker[AsyncSession])
    upserted = 0
    async with session_maker() as session:
        stmt = (
            select(ListingPage, University)
            .outerjoin(University, ListingPage.university_id == University.id)
            .where(ListingPage.schema_status == "ok")
            # mai scrapate prima, poi le più vecchie; a parità, aggregator e atenei famosi
            .order_by(
                ListingPage.last_scraped_at.asc().nulls_first(),
                func.coalesce(University.sitelinks, 10**6).desc(),
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
                    strategy = JsonCssExtractionStrategy(dict(page.extraction_schema or {}))
                    config = CrawlerRunConfig(
                        cache_mode=CacheMode.BYPASS,
                        check_robots_txt=True,
                        extraction_strategy=strategy,
                    )
                    raw_items = await _fetch_items(crawler, page, config)
                    page.last_scraped_at = datetime.now(UTC).replace(tzinfo=None)
                    if raw_items is None:  # fetch fallito: non è colpa dello schema, riprova al prossimo run
                        print(f"scrape fetch failed for {page.url}")
                        await session.commit()
                        continue
                    normalized = [
                        (n, item) for item in raw_items if (n := normalize_item(item, base_url=page.url)) is not None
                    ]
                    if not normalized:
                        page.schema_status = "stale"  # schema rotto o pagina cambiata
                        await session.commit()
                        continue
                    for n, item in normalized:
                        values = _position_values(n, page)
                        if page.kind == "aggregator":
                            # EURAXESS appende il canale di pubblicazione: "Uppsala Universitet via MyNetwork"
                            institution = str(item.get("institution") or "").split(" via ")[0].strip()[:512]
                            uni_id = await _resolve_university(session, institution) if institution else None
                            values |= {
                                "university_id": uni_id,
                                "institution_name": None if uni_id else (institution or None),
                                "institution_country": None if uni_id else _country_code(item.get("country")),
                            }
                        else:
                            # institution_* azzerati: l'url può essere stato visto prima via aggregatore
                            values |= {
                                "university_id": page.university_id,
                                "institution_name": None,
                                "institution_country": None,
                            }
                        upsert = (
                            pg_insert(Position)
                            .values(**values)
                            .on_conflict_do_update(
                                index_elements=["url"],
                                set_={
                                    **{k: v for k, v in values.items() if k != "url"},
                                    # ponytail: re-indicizza (e ri-embedda) a ogni scrape anche se invariato;
                                    # guard IS DISTINCT FROM se il costo embedding diventa rilevante
                                    "indexed_at": None,
                                    "scraped_at": func.now(),  # l'onupdate ORM non scatta sugli upsert Core
                                },
                            )
                        )
                        await session.execute(upsert)
                    upserted += len(normalized)
                    print(f"scrape: {page.url}: {len(normalized)} positions")
                except Exception as exc:
                    print(f"scrape failed for {page.url}: {exc}")
                    await session.rollback()  # la sessione può essere invalida dopo un errore DB
                    page.schema_status = "stale"
                    page.last_scraped_at = datetime.now(UTC).replace(tzinfo=None)
                await session.commit()
    print(f"scrape: {upserted} positions upserted")
    return upserted
