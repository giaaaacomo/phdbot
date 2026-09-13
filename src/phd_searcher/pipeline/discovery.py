"""Stadio 2: per ogni ateneo pending, trova la pagina che elenca i bandi PhD.

Strategia a imbuto, senza LLM fino alla scelta finale: link della homepage →
sitemap.xml → un hop dentro le pagine "hub" (research/careers/postgraduate...).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urljoin, urlparse, urlsplit
from xml.etree import ElementTree

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
from phd_searcher.pipeline.curated_sources import seed_curated_sources
from phd_searcher.pipeline.discovery_selection import DiscoverySelectionExhaustedError, select_listings
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.retry import retry_async
from phd_searcher.pipeline.urls import is_listing_page_url
from phd_searcher.pipeline.workday import recruitment_referrer, workday_board

# ponytail: lista keyword multilingua a mano; estendere se un paese resta scoperto
_KEYWORDS = (
    "phd",
    "jobs",
    "careers",
    "emploi",
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
    "studentship",
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
    "admission",
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
_MAX_SITEMAPS = 4
_MAX_SITEMAP_BYTES = 2_000_000
_LOGGER = logging.getLogger(__name__)


class _Link:
    __slots__ = ("href", "referrer", "text")

    def __init__(self, href: str, text: str, referrer: str = "") -> None:
        self.href = href
        self.text = text
        self.referrer = referrer


def _candidates(links: list[dict[str, str | None]], referrer: str = "") -> list[_Link]:
    out: list[_Link] = []
    seen: set[str] = set()
    for link in links:
        href = link.get("href") or ""
        text = link.get("text") or ""
        haystack = f"{href} {text}".lower()
        project_listing = "project" in haystack and "admission" in haystack
        if (
            href
            and href not in seen
            and is_listing_page_url(href)
            and (project_listing or any(k in haystack for k in _KEYWORDS))
        ):
            seen.add(href)
            out.append(_Link(href, text.strip()[:120], referrer))
    return out[:_MAX_CANDIDATES]


def _hub_links(links: list[dict[str, str | None]]) -> list[str]:
    scored: list[tuple[int, str]] = []
    for link in links:
        href = link.get("href") or ""
        haystack = f"{href} {link.get('text') or ''}".lower()
        if not href.startswith("http") or not is_listing_page_url(href) or any(x in haystack for x in _HUB_EXCLUDE):
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
    """Read a bounded sitemap index, including namespaced XML and subdomains.

    A root /en/ homepage must not turn /sitemap.xml into /en/sitemap.xml.
    Four requests total, only same-site redirects/index traversal, and a
    streaming size limit keep discovery independent of a site's archive size.
    """
    queue = [urljoin(website_url, "/sitemap.xml")]
    visited: set[str] = set()
    groups: list[list[_Link]] = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        while queue and len(visited) < _MAX_SITEMAPS:
            url = queue.pop(0)
            if url in visited or not _public_sitemap_url(url, website_url):
                continue
            visited.add(url)
            try:
                payload = bytearray()
                async with client.stream("GET", url) as response:
                    _LOGGER.debug("discovery sitemap %s: HTTP %s", url, response.status_code)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        destination = urljoin(url, response.headers.get("location", ""))
                        if destination not in visited and _public_sitemap_url(destination, website_url):
                            queue.insert(0, destination)
                        continue
                    if response.status_code != 200:
                        continue
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > _MAX_SITEMAP_BYTES:
                            break
                if len(payload) > _MAX_SITEMAP_BYTES or b"<!DOCTYPE" in payload.upper():
                    continue
                root = ElementTree.fromstring(payload)
            except (httpx.HTTPError, ElementTree.ParseError) as exc:
                _LOGGER.warning("discovery sitemap unavailable %s: %s", url, exc)
                continue
            kind = root.tag.rsplit("}", 1)[-1]
            entries = [
                node.text.strip()
                for entry in root
                for node in entry
                if node.tag.rsplit("}", 1)[-1] == "loc" and node.text
            ]
            if kind == "sitemapindex":
                # Recruitment/post sitemaps before image/tag archives. Preserve
                # publisher order among equal priorities and cap queued work.
                entries.sort(key=lambda u: not any(k in u.casefold() for k in (*_KEYWORDS, "post", "page")))
                queue.extend(u for u in entries if u not in visited and _public_sitemap_url(u, website_url))
                queue = list(dict.fromkeys(queue))[: _MAX_SITEMAPS - len(visited)]
            elif kind == "urlset":
                groups.append(
                    _candidates([{"href": u, "text": ""} for u in entries if _public_sitemap_url(u, website_url)])
                )
    return _merge_candidate_groups(groups)


def _public_sitemap_url(url: str, website_url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.username is None
        and parsed.password is None
        and _same_site(url, website_url)
    )


async def _collect_candidates(
    crawler: AsyncWebCrawler, config: CrawlerRunConfig, website_url: str, links: list[dict[str, str | None]]
) -> list[_Link]:
    """Unione dei candidati da homepage, sitemap e un hop nelle pagine hub.

    Tutti i livelli sempre: i candidati della sola homepage sono spesso pagine
    informative che l'LLM scarta, mentre il listing vero è un hop più in là.
    """
    groups = [_candidates(links, website_url), await _sitemap_candidates(website_url)]
    for hub in _hub_links(links):
        result = await crawler.arun(hub, config=config)
        if result.success:
            hub_links = list(result.links.get("internal", [])) + list(result.links.get("external", []))
            groups.append(_candidates(hub_links, result.redirected_url or hub))
    merged = _merge_candidate_groups(groups)
    for candidate in merged:
        # A duplicate link from a homepage/sitemap must not erase stronger
        # provenance subsequently observed on an official recruitment hub.
        if workday_board(candidate.href):
            for group in groups:
                for observed in group:
                    if observed.href == candidate.href and recruitment_referrer(observed.referrer, website_url):
                        candidate.referrer = observed.referrer
    return merged


def _merge_candidate_groups(groups: list[list[_Link]]) -> list[_Link]:
    """Share the fixed candidate budget across homepage, sitemap and hubs.

    Concatenation followed by truncation discarded every department link when
    a homepage already supplied 30 candidates, despite fetching all the hubs.
    Round-robin preserves their representation without extra requests, a
    larger prompt, or automatically trusting any page as a vacancy listing.
    Duplicate links do not consume another group's turn.
    """
    iterators = [iter(group) for group in groups]
    merged: dict[str, _Link] = {}
    while iterators and len(merged) < _MAX_CANDIDATES:
        active = []
        for candidates in iterators:
            for candidate in candidates:
                if candidate.href not in merged:
                    merged[candidate.href] = candidate
                    active.append(candidates)
                    break
            if len(merged) == _MAX_CANDIDATES:
                break
        iterators = active
    return list(merged.values())


def _parse_reply(reply: str, allowed: set[str]) -> list[str]:
    """Distinguish a valid empty selection from an invalid model response.

    Returning [] for malformed output labelled the institution ``no_listing``
    and postponed its retry for 30 days. Raise instead: the run records a
    technical discovery failure, preserves existing sources and can retry.
    """
    cleaned = reply.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(cleaned)
    except ValueError as exc:
        raise RuntimeError("discovery selection is not valid JSON; retry discovery") from exc
    if not isinstance(parsed, list) or any(not isinstance(u, str) for u in parsed):
        raise RuntimeError("discovery selection is not a URL list; retry discovery")
    selected = list(dict.fromkeys(u for u in parsed if u in allowed))
    if parsed and not selected:
        raise RuntimeError("discovery selected no supplied URLs; retry discovery")
    return selected


def _stop_requested(progress: Progress) -> bool:
    """Read the mutable stop flag without letting type narrowing span awaits."""
    return progress.should_stop


def _select_with_supported_boards(reply: str, candidates: list[_Link], website: str) -> list[str]:
    supported = [
        c.href for c in candidates
        if workday_board(c.href) and recruitment_referrer(c.referrer, website)
    ]
    try:
        selected = _parse_reply(reply, {c.href for c in candidates})
    except RuntimeError:
        if not supported:
            raise
        # The optional model selection must not discard a supported portal
        # already linked by the institution. Schema admission still verifies
        # its live link and scope; ambiguous candidates are not auto-approved.
        _LOGGER.warning("discovery: invalid model selection; retaining supported recruitment boards only")
        selected = []
    return list(dict.fromkeys([*supported, *selected]))


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
                        & (University.discovery_checked_at.is_(None) | (University.discovery_checked_at < done_before))
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
                    # Some institutions expose an official central portal that
                    # generic ranking can miss among many departmental pages.
                    # The audited registry complements rather than replaces
                    # normal discovery and carries a validated deterministic schema.
                    await seed_curated_sources(session, uni)

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

                        async def select_current_candidates(
                            current_prompt: str = prompt,
                            allowed: frozenset[str] = frozenset(c.href for c in candidates),
                        ) -> list[str]:
                            return await select_listings(model, current_prompt, allowed, progress)

                        try:
                            selected = await retry_async(
                                progress, f"discovery:{uni.id}:llm", select_current_candidates,
                                non_retryable=(DiscoverySelectionExhaustedError,),
                            )
                        except DiscoverySelectionExhaustedError:
                            # Preserve the existing conservative supported-board fallback.
                            valid = _select_with_supported_boards("invalid", candidates, uni.website_url)
                        else:
                            valid = _select_with_supported_boards(json.dumps(selected), candidates, uni.website_url)
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
                                        quality_metrics={
                                            "discovery_referrer": next((c.referrer for c in candidates if c.href == u), ""),
                                        },
                                    )
                                )
                                referrer = next((c.referrer for c in candidates if c.href == u), "")
                                # Refresh provenance only for this same owner. Never steal
                                # another institution's source on a shared recruitment site.
                                if referrer and recruitment_referrer(referrer, uni.website_url) and workday_board(u):
                                    stmt_lp = stmt_lp.on_conflict_do_update(
                                        index_elements=["url"],
                                        set_={
                                            "quality_metrics": ListingPage.quality_metrics.op("||")({"discovery_referrer": referrer}),
                                            "schema_status": case(
                                                ((ListingPage.schema_status == "deferred") & (ListingPage.quality_reason == "source_preflight:ownership_unverified"), "missing"),
                                                else_=ListingPage.schema_status,
                                            ),
                                        },
                                        where=ListingPage.university_id == uni.id,
                                    )
                                else:
                                    stmt_lp = stmt_lp.on_conflict_do_nothing(index_elements=["url"])
                                await session.execute(stmt_lp)
                            uni.discovery_status = "done"
                            found += 1
                except Exception as exc:  # un sito rotto non ferma la run
                    if _stop_requested(progress):
                        break
                    _LOGGER.error("discovery failed for %s: %s", uni.name, exc)
                    await progress.save_checkpoint(last_error=str(exc)[:1000], failed_university_id=uni.id)
                    await session.rollback()  # la sessione può essere invalida dopo un errore DB
                    # Retry a failed refresh promptly as well. Existing
                    # listing rows are retained and remain scrapeable.
                    uni.discovery_status = "failed"
                uni.discovery_checked_at = datetime.now(UTC).replace(tzinfo=None)
                await session.commit()
                processed += 1
                await progress.save_checkpoint(processed=processed, found=found, last_university_id=uni.id)
    print(f"discovery: {found} found ({len(unis)} attempted)")
    return found
