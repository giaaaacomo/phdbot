"""Stadio 2: per ogni ateneo pending, trova la pagina che elenca i bandi PhD.

Strategia a imbuto, senza LLM fino alla scelta finale: link della homepage →
sitemap.xml → un hop dentro le pagine "hub" (research/careers/postgraduate...).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlparse

import httpx
from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig
from crawl4ai.models import CrawlResult
from injector import Injector
from sqlalchemy import case, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.config.search import SearchConfig
from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.university import University
from phd_searcher.engine.model_helper import ModelHelper
from phd_searcher.engine.prompt_helper import render_prompt
from phd_searcher.engine.search_helper import search_listing_candidates
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.retry import retry_async
from phd_searcher.pipeline.urls import is_listing_page_url

# ponytail: lista keyword multilingua a mano; estendere se un paese resta scoperto
_KEYWORDS = (
    "phd",
    "doctoral",
    "doctorate",
    "vacanc",
    "position",
    "recruit",
    "dottorato",
    "bandi",
    "concorsi",
    "promotie",
    "promovendus",
    "vacature",
    "stellenangebote",
    "doktorand",
    "promotion",
    "doctorat",
    "thèse",
    "these",
    "doctorado",
    "doutoramento",
    "postdoc",
    "assistantship",
    "internship",
    "traineeship",
    "tirocinio",
    "praktikum",
    "fellowship",
    "research-fellow",
    "researcher",
    "assegni",
    "borsa-di-ricerca",
    "mph",
)
_MAX_CANDIDATES = 30
_RECHECK_DONE_AFTER = timedelta(days=7)
_RECHECK_NO_LISTING_AFTER = timedelta(days=30)

# Pagine "hub" da esplorare (un solo hop) quando la homepage non ha link diretti ai bandi.
_HUB_KEYWORDS = (
    "research",
    "job",
    "career",
    "vacan",
    "postgraduate",
    "graduate",
    "phd",
    "doctora",
    "dottorato",
    "lavora",
    "carriere",
    "stellen",
    "karriere",
    "forschung",
    "emploi",
    "recherche",
    "empleo",
    "investigacion",
    "onderzoek",
    "vacature",
    "werken",
)
_MAX_HUBS = 4
_HUB_EXCLUDE = ("news", "event", "story", "press", "alumni")
_SPONTANEOUS_KEYWORDS = (
    "spontaneous application",
    "speculative application",
    "open application",
    "unsolicited application",
    "expression of interest",
    "candidatura spontanea",
    "initiativbewerbung",
)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")


class _Link:
    __slots__ = ("href", "text")

    def __init__(self, href: str, text: str) -> None:
        self.href = href
        self.text = text


def _candidates(links: list[dict[str, str | None]]) -> list[_Link]:
    out: list[_Link] = []
    seen: set[str] = set()
    for link in links:
        href = link.get("href") or ""
        text = link.get("text") or ""
        haystack = f"{href} {text}".lower()
        if href and href not in seen and is_listing_page_url(href) and any(k in haystack for k in _KEYWORDS):
            seen.add(href)
            out.append(_Link(href, text.strip()[:120]))
    return out[:_MAX_CANDIDATES]


def _hub_links(links: list[dict[str, str | None]]) -> list[str]:
    scored: list[tuple[int, str]] = []
    for link in links:
        href = link.get("href") or ""
        haystack = f"{href} {link.get('text') or ''}".lower()
        if not href.startswith("http") or any(x in haystack for x in _HUB_EXCLUDE):
            continue
        if any(k in haystack for k in _HUB_KEYWORDS):
            scored.append((len(href), href))
    scored.sort()  # URL corti prima: più probabile siano radici di sezione, non pagine foglia
    out: list[str] = []
    for _, href in scored:
        if href not in out:
            out.append(href)
    return out[:_MAX_HUBS]


def _spontaneous_application(links: list[dict[str, str | None]]) -> str | None:
    for link in links:
        href = link.get("href") or ""
        haystack = f"{href} {link.get('text') or ''}".casefold()
        if href.startswith("http") and any(keyword in haystack for keyword in _SPONTANEOUS_KEYWORDS):
            return href[:2048]
    return None


def _same_site(url: str, website_url: str) -> bool:
    """True per il dominio ufficiale e suoi sottodomini, normalizzando ``www``.

    I risultati del motore di ricerca non sono link attestati dal sito ufficiale:
    accettare domini arbitrari associa aggregatori o altri atenei all'istituzione.
    """

    def hostname(value: str) -> str:
        return (urlparse(value).hostname or "").casefold().removeprefix("www.")

    candidate = hostname(url)
    official = hostname(website_url)
    return bool(candidate and official) and (candidate == official or candidate.endswith(f".{official}"))


async def _sitemap_candidates(website_url: str) -> list[_Link]:
    """URL keyword-matching dal sitemap.xml (se esiste; gli indici di sitemap sono ignorati)."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(website_url.rstrip("/") + "/sitemap.xml")
            if resp.status_code != 200:
                return []
            urls = _LOC_RE.findall(resp.text)
    except httpx.HTTPError:
        return []
    return _candidates([{"href": u, "text": ""} for u in urls if not u.endswith(".xml")])


async def _collect_candidates(
    crawler: AsyncWebCrawler, config: CrawlerRunConfig, website_url: str, links: list[dict[str, str | None]]
) -> list[_Link]:
    """Unione dei candidati da homepage, sitemap e un hop nelle pagine hub.

    Tutti i livelli sempre: i candidati della sola homepage sono spesso pagine
    informative che l'LLM scarta, mentre il listing vero è un hop più in là.
    """
    merged: dict[str, _Link] = {c.href: c for c in _candidates(links)}
    for c in await _sitemap_candidates(website_url):
        merged.setdefault(c.href, c)
    for hub in _hub_links(links):
        result = await crawler.arun(hub, config=config)
        if result.success:
            hub_links = list(result.links.get("internal", [])) + list(result.links.get("external", []))
            for c in _candidates(hub_links):
                merged.setdefault(c.href, c)
    return list(merged.values())[:_MAX_CANDIDATES]


def _parse_reply(reply: str, allowed: set[str]) -> list[str]:
    """Estrae l'array JSON di URL dalla risposta LLM, scartando allucinazioni. [] su qualunque errore."""
    cleaned = reply.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(cleaned)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [u for u in parsed if isinstance(u, str) and u in allowed]


def _stop_requested(progress: Progress) -> bool:
    """Read the mutable stop flag without letting type narrowing span awaits."""
    return progress.should_stop


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    """Ritorna il numero di atenei passati a `done` (progresso, non tentativi)."""
    progress = progress or Progress()
    session_maker = container.get(async_sessionmaker[AsyncSession])
    model = container.get(ModelHelper)
    search_config = container.get(SearchConfig)
    crawl_config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, check_robots_txt=True)
    checkpoint = await progress.load_checkpoint()
    found = int(cast("int | str", checkpoint.get("found", 0)))
    processed = int(cast("int | str", checkpoint.get("processed", 0)))
    remaining = None if limit is None else max(limit - processed, 0)
    now = datetime.now(UTC).replace(tzinfo=None)
    done_before = now - _RECHECK_DONE_AFTER
    no_listing_before = now - _RECHECK_NO_LISTING_AFTER

    async with session_maker() as session:
        stmt = (
            select(University)
            .where(
                or_(
                    University.discovery_status.in_(("pending", "failed")),
                    (
                        (University.discovery_status == "done")
                        & (
                            University.discovery_checked_at.is_(None)
                            | (University.discovery_checked_at < done_before)
                        )
                    ),
                    (
                        (University.discovery_status == "no_listing")
                        & (
                            University.discovery_checked_at.is_(None)
                            | (University.discovery_checked_at < no_listing_before)
                        )
                    ),
                )
            )
            # prima i pending (i failed sono retry), poi i più famosi
            .order_by(case((University.discovery_status == "pending", 0), else_=1), University.sitelinks.desc())
        )
        if name_like:
            stmt = stmt.where(University.name.ilike(f"%{name_like}%"))
        if remaining is not None:
            stmt = stmt.limit(remaining)
        unis = (await session.execute(stmt)).scalars().all()
        await progress.begin(len(unis))

        # ponytail: sequenziale, un ateneo alla volta; parallelizzare per-dominio se la run completa è troppo lenta
        async with AsyncWebCrawler() as crawler:
            for uni in unis:
                if progress.should_stop:
                    break
                await progress.tick(uni.name)
                if _stop_requested(progress):
                    break
                was_done = uni.discovery_status == "done"
                try:
                    async def crawl_root(website_url: str = uni.website_url) -> CrawlResult:
                        result = await crawler.arun(website_url, config=crawl_config)
                        if not result.success:
                            raise RuntimeError(result.error_message or "crawl failed")
                        return result

                    result = await retry_async(progress, f"discovery:{uni.id}:root", crawl_root)
                    links = list(result.links.get("internal", [])) + list(result.links.get("external", []))
                    uni.spontaneous_application_url = _spontaneous_application(links)

                    async def collect_current_candidates(
                        website_url: str = uni.website_url,
                        current_links: list[dict[str, str | None]] = links,
                    ) -> list[_Link]:
                        return await _collect_candidates(
                            crawler,
                            crawl_config,
                            website_url,
                            current_links,
                        )

                    candidates = await retry_async(
                        progress,
                        f"discovery:{uni.id}:candidates",
                        collect_current_candidates,
                    )

                    domain = urlparse(uni.website_url).netloc

                    async def search_current_domain(current_domain: str = domain) -> list[str]:
                        return await search_listing_candidates(search_config, current_domain)

                    search_urls = await retry_async(
                        progress,
                        f"discovery:{uni.id}:search",
                        search_current_domain,
                    )
                    have = {c.href for c in candidates}
                    search_hrefs = {
                        u
                        for u in search_urls
                        if u not in have and is_listing_page_url(u) and _same_site(u, uni.website_url)
                    }
                    candidates += [_Link(u, "(from web search)") for u in search_hrefs]

                    if not candidates:
                        if not was_done:
                            uni.discovery_status = "no_listing"
                    else:
                        prompt = render_prompt("pick_listings.prompt.jinja", university=uni.name, candidates=candidates)

                        async def complete_current_prompt(current_prompt: str = prompt) -> str:
                            return await model.complete(
                                [{"role": "user", "content": current_prompt}]
                            )

                        reply = await retry_async(
                            progress,
                            f"discovery:{uni.id}:llm",
                            complete_current_prompt,
                        )
                        valid = _parse_reply(reply, {c.href for c in candidates})
                        if not valid:
                            if not was_done:
                                uni.discovery_status = "no_listing"
                        else:
                            for u in valid:
                                stmt_lp = (
                                    pg_insert(ListingPage)
                                    .values(
                                        university_id=uni.id,
                                        url=u[:2048],
                                        kind="university",
                                        source="search" if u in search_hrefs else "funnel",
                                    )
                                    .on_conflict_do_nothing(index_elements=["url"])
                                )
                                await session.execute(stmt_lp)
                            uni.discovery_status = "done"
                            found += 1
                except Exception as exc:  # un sito rotto non ferma la run
                    if _stop_requested(progress):
                        break
                    print(f"discovery failed for {uni.name}: {exc}")
                    await session.rollback()  # la sessione può essere invalida dopo un errore DB
                    if not was_done:
                        uni.discovery_status = "failed"
                uni.discovery_checked_at = datetime.now(UTC).replace(tzinfo=None)
                await session.commit()
                processed += 1
                await progress.save_checkpoint(processed=processed, found=found, last_university_id=uni.id)
    print(f"discovery: {found} found ({len(unis)} attempted)")
    return found
