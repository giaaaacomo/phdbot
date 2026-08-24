"""Stadio 4: esegue gli schemi cachati (niente LLM), upsert delle Position."""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlparse

from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy
from injector import Injector
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.countries import country_code
from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.pipeline.normalize import NormalizedPosition, normalize_item
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.retry import clear_retry, retry_async
from phd_searcher.pipeline.urls import is_listing_page_url

_SAFETY_MAX_PAGES = 1500
_MISSING_RUNS_BEFORE_INACTIVE = 2
_EURAXESS_HOST = "euraxess.ec.europa.eu"
_EURAXESS_PAGE_DELAY = 6.0
_EURAXESS_RATE_LIMIT_COOLDOWN = 300.0
_DNS_FAILURE_MARKERS = (
    "ERR_NAME_NOT_RESOLVED",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_CHANGED",
    "Temporary failure in name resolution",
    "Network is unreachable",
)
_HISTORIC_ROBOTS_DENIAL_MESSAGES = (
    "access denied by robots.txt",
    "url is disallowed by robots.txt",
    "robots.txt denied access to this resource",
    "blocked by robots.txt",
)


class PermanentSourceDenialError(RuntimeError):
    """Diniego strutturale della sorgente: ritentare non può cambiarne l'esito."""


def _checkpoint_int(value: object) -> int:
    return int(value) if isinstance(value, (int, float, str)) else 0


def _checkpoint_ids(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()
    return {
        _checkpoint_int(item)
        for item in value
        if isinstance(item, (int, float, str))
    }


def _should_stop(progress: Progress) -> bool:
    return progress.should_stop


async def _internet_dns_available() -> bool:
    """Distingue un hostname rotto da una perdita generale della rete/DNS."""
    for host in ("euraxess.ec.europa.eu", "www.wikidata.org"):
        try:
            await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, 443), timeout=5)
        except (TimeoutError, OSError):
            continue
        return True
    return False


async def _network_is_unavailable(exc: Exception) -> bool:
    message = str(exc)
    if not any(marker in message for marker in _DNS_FAILURE_MARKERS):
        return False
    return not await _internet_dns_available()


def _is_permanent_source_denial(exc: Exception) -> bool:
    """Compatibilità per retry storici salvati prima dell'eccezione tipizzata."""
    message = " ".join(str(exc).casefold().split())
    return any(
        message == known or message.startswith((f"{known}: ", f"{known} - "))
        for known in _HISTORIC_ROBOTS_DENIAL_MESSAGES
    )


async def _fetch_page(
    crawler: AsyncWebCrawler,
    page: ListingPage,
    config: CrawlerRunConfig,
    *,
    page_number: int,
    progress: Progress,
) -> list[dict[str, object]]:
    """Scarica una singola pagina con retry durevole; il chiamante esegue commit/checkpoint."""
    sep = "&" if "?" in page.url else "?"
    url = f"{page.url}{sep}{page.pagination_param}={page_number}" if page.pagination_param else page.url

    async def fetch() -> list[dict[str, object]]:
        result = await crawler.arun(url, config=config)
        if not result.success:
            message = result.error_message or f"fetch failed: {url}"
            status_code = result.redirected_status_code or result.status_code
            if status_code in {401, 403} or _is_permanent_source_denial(RuntimeError(message)):
                raise PermanentSourceDenialError(message)
            raise RuntimeError(message)
        parsed_batch: object = json.loads(result.extracted_content or "[]")
        if not isinstance(parsed_batch, list):
            raise RuntimeError(f"extraction did not return a list: {url}")
        batch = cast(list[object], parsed_batch)
        return [cast(dict[str, object], item) for item in batch if isinstance(item, dict)]

    is_euraxess = urlparse(page.url).hostname == _EURAXESS_HOST
    return await retry_async(
        progress,
        f"scrape:{page.id}:page:{page_number}",
        fetch,
        max_attempts=8 if is_euraxess else 3,
        base_delay=60 if is_euraxess else 10,
        max_delay=900 if is_euraxess else 60,
        # Crawl4AI conserva il testo "HTTP 429" ma non gli header: EURAXESS
        # necessita di un vero cooldown, non di tre retry ravvicinati.
        rate_limit_delay=_EURAXESS_RATE_LIMIT_COOLDOWN if is_euraxess else None,
        non_retryable=(PermanentSourceDenialError,),
    )


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
    """Compatibilità interna per i test e i chiamanti storici."""
    return country_code(raw)


def _position_values(
    n: NormalizedPosition,
    page: ListingPage,
    *,
    observed_at: datetime | None = None,
) -> dict[str, object | None]:
    return {
        "url": n.url[:2048],
        "title": n.title[:1024],
        "description": n.description,
        "area": n.area,
        "language": n.language,
        "duration_raw": n.duration_raw,
        "compensation_raw": n.compensation_raw,
        "compensation_min": n.compensation_min,
        "compensation_max": n.compensation_max,
        "compensation_currency": n.compensation_currency,
        "compensation_period": n.compensation_period,
        "published_raw": n.published_raw,
        "published_at": n.published_at,
        "position_type": n.position_type,
        "research_group": n.research_group,
        "deadline_raw": n.deadline_raw,
        "deadline": n.deadline,
        "listing_page_id": page.id,
        "is_active": True,
        "missing_runs": 0,
        # `now()` di PostgreSQL è l'ora d'inizio della transazione e può essere
        # precedente a source_started_at: usa un timestamp reale per non marcare
        # come assenti record appena visti nello stesso refresh.
        "scraped_at": observed_at or datetime.now(UTC).replace(tzinfo=None),
    }


async def _upsert_items(
    session: AsyncSession,
    page: ListingPage,
    raw_items: list[dict[str, object]],
) -> int:
    normalized = [(n, item) for item in raw_items if (n := normalize_item(item, base_url=page.url)) is not None]
    observed_at = datetime.now(UTC).replace(tzinfo=None)
    for n, item in normalized:
        values = _position_values(n, page, observed_at=observed_at)
        if page.kind == "aggregator":
            institution = str(item.get("institution") or "").split(" via ")[0].strip()[:512]
            uni_id = await _resolve_university(session, institution) if institution else None
            values |= {
                "university_id": uni_id,
                "institution_name": None if uni_id else (institution or None),
                "institution_country": None if uni_id else _country_code(item.get("country")),
            }
        else:
            values |= {
                "university_id": page.university_id,
                "institution_name": None,
                "institution_country": None,
            }
        insert_stmt = pg_insert(Position).values(**values)
        effective_duration = func.coalesce(insert_stmt.excluded.duration_raw, Position.duration_raw)
        effective_compensation = func.coalesce(insert_stmt.excluded.compensation_raw, Position.compensation_raw)
        effective_compensation_min = func.coalesce(
            insert_stmt.excluded.compensation_min, Position.compensation_min
        )
        effective_compensation_max = func.coalesce(
            insert_stmt.excluded.compensation_max, Position.compensation_max
        )
        effective_compensation_currency = func.coalesce(
            insert_stmt.excluded.compensation_currency, Position.compensation_currency
        )
        effective_compensation_period = func.coalesce(
            insert_stmt.excluded.compensation_period, Position.compensation_period
        )
        effective_published_raw = func.coalesce(insert_stmt.excluded.published_raw, Position.published_raw)
        effective_published_at = func.coalesce(insert_stmt.excluded.published_at, Position.published_at)
        effective_research_group = func.coalesce(insert_stmt.excluded.research_group, Position.research_group)
        effective_deadline_raw = func.coalesce(
            insert_stmt.excluded.deadline_raw,
            Position.deadline_raw,
        )
        effective_deadline = case(
            (
                insert_stmt.excluded.deadline_raw.is_not(None),
                insert_stmt.excluded.deadline,
            ),
            else_=Position.deadline,
        )
        effective_position_type = case(
            (insert_stmt.excluded.position_type == "other", Position.position_type),
            else_=insert_stmt.excluded.position_type,
        )
        detail_change = or_(
            Position.title.is_distinct_from(insert_stmt.excluded.title),
            Position.description.is_distinct_from(insert_stmt.excluded.description),
            Position.duration_raw.is_distinct_from(effective_duration),
            Position.compensation_raw.is_distinct_from(effective_compensation),
            Position.published_at.is_distinct_from(effective_published_at),
            Position.position_type.is_distinct_from(effective_position_type),
            Position.research_group.is_distinct_from(effective_research_group),
            Position.university_id.is_distinct_from(insert_stmt.excluded.university_id),
            Position.institution_name.is_distinct_from(insert_stmt.excluded.institution_name),
            Position.institution_country.is_distinct_from(insert_stmt.excluded.institution_country),
            Position.deadline_raw.is_distinct_from(effective_deadline_raw),
            Position.deadline.is_distinct_from(effective_deadline),
        )
        index_change = or_(detail_change, Position.is_active.is_(False))
        auto_rescreen = and_(detail_change, Position.screening_manual.is_(False))
        upsert = insert_stmt.on_conflict_do_update(
            index_elements=["url"],
            set_={
                **{k: v for k, v in values.items() if k != "url"},
                # I campi estratti dalla pagina dettaglio sono più ricchi: una listing
                # che non li espone non deve cancellarli ad ogni refresh.
                "duration_raw": effective_duration,
                "compensation_raw": effective_compensation,
                "compensation_min": effective_compensation_min,
                "compensation_max": effective_compensation_max,
                "compensation_currency": effective_compensation_currency,
                "compensation_period": effective_compensation_period,
                "published_raw": effective_published_raw,
                "published_at": effective_published_at,
                "research_group": effective_research_group,
                "deadline_raw": effective_deadline_raw,
                "deadline": effective_deadline,
                "position_type": effective_position_type,
                "full_description": case((detail_change, None), else_=Position.full_description),
                "details_scraped_at": case((detail_change, None), else_=Position.details_scraped_at),
                "screening_status": case((auto_rescreen, "pending"), else_=Position.screening_status),
                "screening_reason": case((auto_rescreen, None), else_=Position.screening_reason),
                "screening_source": case((auto_rescreen, "rules"), else_=Position.screening_source),
                "screening_decision": case((auto_rescreen, None), else_=Position.screening_decision),
                "screening_confidence": case((auto_rescreen, None), else_=Position.screening_confidence),
                "screening_evidence": case((auto_rescreen, None), else_=Position.screening_evidence),
                "screening_model": case((auto_rescreen, None), else_=Position.screening_model),
                "screening_version": case((auto_rescreen, None), else_=Position.screening_version),
                "screened_at": case((auto_rescreen, None), else_=Position.screened_at),
                "review_state": case((auto_rescreen, "untriaged"), else_=Position.review_state),
                "routing_reason": case((auto_rescreen, None), else_=Position.routing_reason),
                "indexed_at": case((index_change, None), else_=Position.indexed_at),
            },
        )
        await session.execute(upsert)
    return len(normalized)


def _page_budget(page: ListingPage, max_pages: int | None) -> tuple[int, bool]:
    """Numero massimo di pagine e se il completamento richiede una pagina vuota."""
    if not page.pagination_param:
        return 1, False
    if max_pages is not None:
        return max_pages, False
    return _SAFETY_MAX_PAGES, True


async def _record_missing_positions(
    session: AsyncSession,
    page: ListingPage,
    source_started_at: datetime,
) -> int:
    """Incrementa le assenze solo dopo una scansione esaustiva e disattiva alla seconda."""
    new_missing_runs = Position.missing_runs + 1
    result = await session.execute(
        update(Position)
        .where(Position.listing_page_id == page.id, Position.scraped_at < source_started_at)
        .values(
            missing_runs=new_missing_runs,
            is_active=case(
                (new_missing_runs >= _MISSING_RUNS_BEFORE_INACTIVE, False),
                else_=Position.is_active,
            ),
            indexed_at=case(
                (new_missing_runs >= _MISSING_RUNS_BEFORE_INACTIVE, None),
                else_=Position.indexed_at,
            ),
        )
        .returning(Position.id)
    )
    return len(result.scalars().all())


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    max_pages: int | None = None,
    progress: Progress | None = None,
) -> int:
    """Ritorna il numero di posizioni upsertate."""
    progress = progress or Progress()
    session_maker = container.get(async_sessionmaker[AsyncSession])
    checkpoint = await progress.load_checkpoint()
    upserted = _checkpoint_int(checkpoint.get("upserted", 0))
    processed_sources = _checkpoint_int(checkpoint.get("processed_sources", 0))
    completed_source_ids = _checkpoint_ids(checkpoint.get("completed_source_ids", []))
    selected_source_ids = _checkpoint_ids(checkpoint.get("selected_source_ids", []))
    active_source_id = _checkpoint_int(checkpoint.get("active_source_id", 0) or 0)
    next_page = _checkpoint_int(checkpoint.get("next_page", 0))
    raw_source_started_at = checkpoint.get("source_started_at")
    raw_quarantined = checkpoint.get("quarantined_sources", {})
    quarantined_sources = dict(raw_quarantined) if isinstance(raw_quarantined, dict) else {}
    raw_deferred = checkpoint.get("deferred_sources", {})
    deferred_sources = dict(raw_deferred) if isinstance(raw_deferred, dict) else {}
    async with session_maker() as session:
        stmt = (
            select(ListingPage, University)
            .outerjoin(University, ListingPage.university_id == University.id)
            .where(ListingPage.schema_status == "ok")
            # mai scrapate prima, poi le più vecchie; a parità, aggregator e atenei famosi
            .order_by(
                case((ListingPage.kind == "aggregator", 0), else_=1),
                ListingPage.last_scraped_at.asc().nulls_first(),
                func.coalesce(University.sitelinks, 10**6).desc(),
            )
        )
        if name_like:
            stmt = stmt.where(University.name.ilike(f"%{name_like}%"))
        rows: list[tuple[ListingPage, University | None]] = [
            (page, university)
            for page, university in (await session.execute(stmt)).all()
        ]
        unsupported = [(page, uni) for page, uni in rows if not is_listing_page_url(page.url)]
        for page, _ in unsupported:
            page.schema_status = "unsupported"
        if unsupported:
            await session.commit()
        rows = [
            (page, uni)
            for page, uni in rows
            if is_listing_page_url(page.url)
        ]
        if selected_source_ids:
            rows = [(page, uni) for page, uni in rows if page.id in selected_source_ids]
        else:
            if limit is not None:
                rows = rows[:limit]
            selected_source_ids = {page.id for page, _ in rows}
            await progress.save_checkpoint(selected_source_ids=sorted(selected_source_ids))
        rows = [(page, uni) for page, uni in rows if page.id not in completed_source_ids]
        await progress.begin(len(rows))

        async with AsyncWebCrawler() as crawler:
            for page, uni in rows:
                if _should_stop(progress):
                    break
                await progress.tick(uni.name if uni else page.url)
                if progress.should_stop:
                    break
                strategy = JsonCssExtractionStrategy(dict(page.extraction_schema or {}))
                config = CrawlerRunConfig(
                    cache_mode=CacheMode.BYPASS,
                    check_robots_txt=True,
                    extraction_strategy=strategy,
                )
                start_page = next_page if active_source_id == page.id else 0
                page_budget, require_empty_page = _page_budget(page, max_pages)
                page_stop = start_page + page_budget if require_empty_page else page_budget
                if active_source_id == page.id and isinstance(raw_source_started_at, str):
                    try:
                        source_started_at = datetime.fromisoformat(raw_source_started_at)
                    except ValueError:
                        source_started_at = datetime.now(UTC).replace(tzinfo=None)
                else:
                    source_started_at = datetime.now(UTC).replace(tzinfo=None)
                raw_source_started_at = source_started_at.isoformat()
                await progress.save_checkpoint(
                    active_source_id=page.id,
                    next_page=start_page,
                    source_started_at=raw_source_started_at,
                )
                source_finished = False
                exhaustive_source = False
                source_skipped = False
                for page_number in range(start_page, page_stop):
                    await progress.check_stop()
                    if _should_stop(progress):
                        break
                    try:
                        raw_items = await _fetch_page(
                            crawler,
                            page,
                            config,
                            page_number=page_number,
                            progress=progress,
                        )
                    except Exception as exc:
                        if _should_stop(progress):
                            break
                        print(f"scrape fetch failed for {page.url} page {page_number}: {exc}")
                        if await _network_is_unavailable(exc):
                            raise RuntimeError(
                                f"network unavailable while scraping {page.url}; resume when connectivity returns"
                            ) from exc
                        if isinstance(exc, PermanentSourceDenialError) or _is_permanent_source_denial(exc):
                            retry_key = f"scrape:{page.id}:page:{page_number}"
                            reason = str(exc)[:1000]
                            print(f"scrape: quarantining {page.url}: {reason}")
                            page.schema_status = "blocked"
                            await session.commit()
                            quarantined_sources[str(page.id)] = {
                                "url": page.url,
                                "reason": reason,
                                "at": datetime.now(UTC).isoformat(),
                            }
                            await clear_retry(progress, retry_key)
                            await progress.save_checkpoint(quarantined_sources=quarantined_sources)
                            source_finished = True
                            source_skipped = True
                            break
                        retry_key = f"scrape:{page.id}:page:{page_number}"
                        reason = str(exc)[:1000]
                        print(f"scrape: deferring unreachable source {page.url}: {reason}")
                        deferred_sources[str(page.id)] = {
                            "url": page.url,
                            "page": page_number,
                            "reason": reason,
                            "at": datetime.now(UTC).isoformat(),
                        }
                        await clear_retry(progress, retry_key)
                        await progress.save_checkpoint(deferred_sources=deferred_sources)
                        source_finished = True
                        source_skipped = True
                        break
                    if not raw_items:
                        if page_number == 0:
                            page.schema_status = "stale"
                        source_finished = True
                        exhaustive_source = page_number > 0
                        await session.commit()
                        break
                    try:
                        count = await _upsert_items(session, page, raw_items)
                        if count == 0 and page_number == 0:
                            page.schema_status = "stale"
                            source_finished = True
                        await session.commit()
                    except Exception as exc:
                        await session.rollback()
                        print(f"scrape normalize/upsert failed for {page.url} page {page_number}: {exc}")
                        raise RuntimeError(
                            f"scrape upsert failed for {page.url} page {page_number}; resume to retry it"
                        ) from exc
                    upserted += count
                    next_page = page_number + 1
                    await progress.save_checkpoint(
                        active_source_id=page.id,
                        next_page=next_page,
                        upserted=upserted,
                    )
                    print(f"scrape: {page.url} page {page_number}: {count} positions")
                    if source_finished:
                        break
                    if page.pagination_param:
                        await asyncio.sleep(
                            _EURAXESS_PAGE_DELAY
                            if urlparse(page.url).hostname == _EURAXESS_HOST
                            else 1
                        )
                else:
                    if require_empty_page:
                        raise RuntimeError(
                            f"scrape safety cap of {_SAFETY_MAX_PAGES} pages reached for {page.url}; "
                            "the source was not marked complete"
                        )
                    source_finished = True
                    exhaustive_source = page.pagination_param is None

                if _should_stop(progress):
                    break
                if source_finished and exhaustive_source:
                    missing = await _record_missing_positions(session, page, source_started_at)
                    if missing:
                        print(f"scrape: {page.url}: {missing} positions absent from this refresh")
                if source_finished and not source_skipped:
                    page.last_scraped_at = datetime.now(UTC).replace(tzinfo=None)
                    await session.commit()
                if source_finished:
                    completed_source_ids.add(page.id)
                    processed_sources += 1
                    active_source_id = 0
                    next_page = 0
                    raw_source_started_at = None
                    await progress.save_checkpoint(
                        completed_source_ids=sorted(completed_source_ids),
                        processed_sources=processed_sources,
                        active_source_id=active_source_id,
                        next_page=next_page,
                        source_started_at=raw_source_started_at,
                        upserted=upserted,
                    )
    print(f"scrape: {upserted} positions upserted")
    return upserted
