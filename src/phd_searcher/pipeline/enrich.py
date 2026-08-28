"""Recupero durevole delle pagine di dettaglio.

Lo stesso motore serve due stadi distinti: ``evidence`` porta testo nuovo ai
casi ancora in review senza promuoverli; ``enrich`` completa i soli eligible.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag
from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig
from crawl4ai.models import CrawlResult
from injector import Injector
from pypdf import PdfReader
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.clock import local_today
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.review_attempt import ReviewAttempt
from phd_searcher.database.models.university import University
from phd_searcher.pipeline.normalize import (
    extract_deadline,
    extract_research_group,
    extract_terms,
    parse_compensation,
    parse_deadline,
)
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.retry import RetryInterruptedError, clear_retry, retry_async
from phd_searcher.pipeline.review_audit import append_review_attempt
from phd_searcher.pipeline.review_context import (
    classify_opportunity_kind_evidence,
    select_evidence_document,
)
from phd_searcher.pipeline.rule_sweep import RULE_SWEEP_VERSION, apply_rule_sweep
from phd_searcher.position_types import classify_position
from phd_searcher.screening import (
    ScreeningDecision,
    detail_rejection_evidence,
    screen_enriched_position,
    screen_position,
)

_EURAXESS_HOST: str = "euraxess.ec.europa.eu"
_EURAXESS_DETAIL_DELAY: float = 2.5
_EURAXESS_RATE_LIMIT_COOLDOWN: float = 300.0
_EURAXESS_MAX_COOLDOWN: float = 3600.0
_DEFERRED_INTERLEAVE: int = 20
_WAIT_CHUNK: float = 30.0
_MIN_INLINE_EVIDENCE_CHARS: int = 200
_MAX_FRAGMENT_EVIDENCE_CHARS: int = 20_000
# Synthetic fragments are generated from a listing item title.  Unlike a real
# DOM id, they can only fall back to an inferred container, so keep that
# container deliberately smaller than a complete vacancy catalogue.
_MAX_SYNTHETIC_FRAGMENT_EVIDENCE_CHARS: int = 6_000
_MAX_SYNTHETIC_TITLE_OFFSET: int = 1_000
_MAX_DIRECT_DOCUMENT_BYTES: int = 40 * 1024 * 1024
_MAX_PDF_EVIDENCE_CHARS: int = 200_000
_DETAIL_GUARD_VERSION = "detail-guard-v2"
_DETAIL_FETCH_RETRY_VERSION = "v4"
_LEGACY_REVALIDATION_VERSIONS = ("hybrid-v2", "hybrid-v3", "hybrid-v4")
_DURABLE_EVIDENCE_STATES = ("needs_evidence", "semantic_uncertain", "fetch_failed")
_FRAGMENT_CONTAINER_HINT = re.compile(
    r"(?:accordion|call|card|item|job|opening|opportunit|panel|position|vacanc)",
    re.I,
)


class _UnsupportedDirectDocumentError(RuntimeError):
    """A download URL returned a format that this evidence stage cannot parse."""


class _AccessBlockedError(RuntimeError):
    """The remote host rejected this run; immediate retries would be wasteful."""


def _markdown(result: CrawlResult) -> str:
    markdown = result.markdown
    if isinstance(markdown, str):
        return markdown.strip()
    for attribute in ("fit_markdown", "raw_markdown"):
        value = getattr(markdown, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_euraxess_url(url: str) -> bool:
    return urlsplit(url).hostname == _EURAXESS_HOST


def _looks_like_direct_document(url: str) -> bool:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/").casefold()
    return path.endswith((".pdf", "/download")) or "download=" in parsed.query.casefold()


def _is_supported_detail_url(url: str) -> bool:
    return urlsplit(url).scheme.casefold() in {"http", "https"}


def _looks_like_unsupported_asset(url: str) -> bool:
    path = urlsplit(url).path.casefold()
    return path.endswith((".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"))


def _apply_extracted_detail_metadata(
    position: Position,
    full_description: str,
) -> None:
    """Fill detail-only metadata without erasing richer stored values."""
    compensation, duration, published = extract_terms(full_description)
    if compensation and not position.compensation_raw:
        position.compensation_raw = compensation
        (
            position.compensation_min,
            position.compensation_max,
            position.compensation_currency,
            position.compensation_period,
        ) = parse_compensation(compensation)
    if duration and not position.duration_raw:
        position.duration_raw = duration
    if published and not position.published_raw:
        position.published_raw = published
        position.published_at = parse_deadline(published)
    if position.deadline is None:
        deadline_raw, deadline = extract_deadline(full_description)
        if deadline is not None:
            position.deadline_raw = deadline_raw
            position.deadline = deadline
    if not position.research_group:
        position.research_group = extract_research_group(full_description)


def _is_access_block(message: str) -> bool:
    folded = message.casefold()
    return any(
        marker in folded
        for marker in (
            "http 401",
            "http 403",
            "access denied",
            "captcha",
        )
    )


def _extract_pdf_text(payload: bytes) -> str:
    if len(payload) > _MAX_DIRECT_DOCUMENT_BYTES:
        raise _UnsupportedDirectDocumentError(
            f"PDF exceeds {_MAX_DIRECT_DOCUMENT_BYTES} bytes"
        )
    if not payload.lstrip().startswith(b"%PDF-"):
        raise _UnsupportedDirectDocumentError("download is not a PDF")
    try:
        reader = PdfReader(BytesIO(payload), strict=False)
        text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages)
    except Exception as exc:
        raise _UnsupportedDirectDocumentError(f"PDF parse failed: {exc}") from exc
    compact = text.strip()
    if not compact:
        raise _UnsupportedDirectDocumentError("PDF contains no extractable text")
    return compact[:_MAX_PDF_EVIDENCE_CHARS]


async def _fetch_direct_document(url: str) -> str:
    headers = {"User-Agent": "PHDBOT/0.1 evidence-fetcher"}
    async with (
        httpx.AsyncClient(follow_redirects=True, timeout=90, headers=headers) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > _MAX_DIRECT_DOCUMENT_BYTES:
            raise _UnsupportedDirectDocumentError(
                f"PDF exceeds {_MAX_DIRECT_DOCUMENT_BYTES} bytes"
            )
        content_type = response.headers.get("content-type", "").casefold()
        chunks: list[bytes] = []
        downloaded = 0
        async for chunk in response.aiter_bytes():
            downloaded += len(chunk)
            if downloaded > _MAX_DIRECT_DOCUMENT_BYTES:
                raise _UnsupportedDirectDocumentError(
                    f"PDF exceeds {_MAX_DIRECT_DOCUMENT_BYTES} bytes"
                )
            chunks.append(chunk)
    payload = b"".join(chunks)
    if "application/pdf" not in content_type and not payload.lstrip().startswith(b"%PDF-"):
        raise _UnsupportedDirectDocumentError(
            f"unsupported download content-type: {content_type or 'missing'}"
        )
    return await asyncio.to_thread(_extract_pdf_text, payload)


def _has_url_fragment(url: str) -> bool:
    return bool(urlsplit(url).fragment)


def _fragment_base_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _clean_dom_text(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _attribute_tokens(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(value.split())
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def _bounded_fragment_container(node: Tag) -> bool:
    if node.name in {"article", "details", "li"} or node.get("role") in {
        "article",
        "listitem",
    }:
        return True
    hints = " ".join(
        [
            str(node.get("id") or ""),
            *[str(value) for value in (node.get("class") or [])],
        ]
    )
    return bool(_FRAGMENT_CONTAINER_HINT.search(hints))


def _extract_fragment_document(html: str, *, fragment: str, title: str) -> str | None:
    """Extract only the DOM node attributable to one exact fragment/title.

    Real fragments resolve through id/name/aria-controls. Synthetic fragments
    created by normalization may fall back to one unique exact title node and
    its smallest bounded container; ambiguity always abstains.
    """
    if not html.strip() or not fragment:
        return None
    soup = BeautifulSoup(html, "html.parser")
    key = unquote(fragment)
    target = soup.find(id=key) or soup.find(None, {"name": key})
    controller = soup.find(
        lambda node: isinstance(node, Tag)
        and key in _attribute_tokens(node.get("aria-controls"))
    )
    if isinstance(target, Tag):
        heading = _clean_dom_text(controller) if isinstance(controller, Tag) else ""
        controlled_ids = _attribute_tokens(target.get("aria-controls"))
        if controlled_ids:
            heading = _clean_dom_text(target)
            controlled_targets = [
                candidate
                for controlled in controlled_ids
                if isinstance((candidate := soup.find(id=controlled)), Tag)
            ]
            # One controller spanning multiple panels is not attributable to a
            # single candidate without page-specific knowledge.
            if len(controlled_targets) != 1:
                return None
            target = controlled_targets[0]
        text = _clean_dom_text(target)
        combined = " ".join(part for part in (heading, text) if part).strip()
        if (
            _has_attributable_fragment_evidence(combined, title)
            and len(combined) <= _MAX_FRAGMENT_EVIDENCE_CHARS
        ):
            return combined

    if not key.startswith("position-"):
        return None
    wanted = " ".join(title.split()).casefold()
    exact_nodes = [
        node
        for node in soup.find_all(string=True)
        if " ".join(str(node).split()).casefold() == wanted
    ]
    if len(exact_nodes) != 1:
        return None
    node = exact_nodes[0].parent
    while isinstance(node, Tag) and node.name not in {"body", "html"}:
        text = _clean_dom_text(node)
        title_offset = text.casefold().find(wanted)
        if (
            _bounded_fragment_container(node)
            and _has_attributable_fragment_evidence(text, title)
            and len(text) <= _MAX_SYNTHETIC_FRAGMENT_EVIDENCE_CHARS
            and 0 <= title_offset <= _MAX_SYNTHETIC_TITLE_OFFSET
        ):
            return text
        node = node.parent
    return None


def _is_rate_limit(exc: Exception) -> bool:
    message = str(exc).casefold()
    return "429" in message or "too many requests" in message


def _is_browser_download(exc: Exception) -> bool:
    """Playwright reports navigation-triggered files instead of returning them."""
    return "download is starting" in str(exc).casefold()


async def _crawl_detail_document(
    crawler: AsyncWebCrawler,
    url: str,
    config: CrawlerRunConfig,
) -> str:
    """Fetch HTML, falling back when Playwright discovers a download.

    Crawl4AI can expose the same Playwright download condition in two ways:
    by raising ``RuntimeError`` or by returning ``success=False`` with the
    message in ``error_message``. Both paths must reach the direct PDF fetcher.
    """
    try:
        result = await crawler.arun(url, config=config)
    except RuntimeError as exc:
        if _is_browser_download(exc):
            return await _fetch_direct_document(url)
        raise
    if not result.success:
        message = result.error_message or f"detail fetch failed: {url}"
        if _is_browser_download(RuntimeError(message)):
            return await _fetch_direct_document(url)
        if _is_access_block(message):
            raise _AccessBlockedError(message)
        raise RuntimeError(message)
    document = _markdown(result)
    if not document:
        raise RuntimeError(f"empty detail page: {url}")
    return document


def _cooldown_seconds(streak: int) -> float:
    """Backoff host-wide: 5, 10, 20, 40, poi 60 minuti."""
    return float(
        min(_EURAXESS_RATE_LIMIT_COOLDOWN * (2 ** max(streak - 1, 0)), _EURAXESS_MAX_COOLDOWN)
    )


def _checkpoint_int(value: object) -> int:
    return int(value) if isinstance(value, (int, str)) else 0


def _has_sufficient_inline_evidence(value: str) -> bool:
    return len(" ".join(value.split())) >= _MIN_INLINE_EVIDENCE_CHARS


def _has_attributable_fragment_evidence(value: str, title: str) -> bool:
    """Require a substantial dossier that explicitly identifies its item."""
    compact_value = " ".join(value.split())
    compact_title = " ".join(title.split("||", 1)[0].split())
    return (
        _has_sufficient_inline_evidence(compact_value)
        and compact_value.casefold() != compact_title.casefold()
        and compact_title.casefold() in compact_value.casefold()
    )


def _has_authoritative_screening(*, manual: bool, source: str, status: str) -> bool:
    return (
        manual or source in {"llm", "router", "cache"} or source.startswith("quality")
    ) and status in {
        "eligible",
        "review",
        "rejected",
        "quarantine",
    }


def _preserve_screening_for_detail(
    *,
    operation: str,
    manual: bool,
    source: str,
    status: str,
) -> bool:
    """Evidence collection may enrich routing, never rewrite the public verdict."""
    return operation == "evidence" or _has_authoritative_screening(
        manual=manual,
        source=source,
        status=status,
    )


def _needs_legacy_revalidation_evidence(position: Position) -> bool:
    """Identify snippet-only legacy verdicts without changing their public status."""
    return (
        not position.screening_manual
        and position.screening_source in {"llm", "cache"}
        and position.screening_version in _LEGACY_REVALIDATION_VERSIONS
        and position.screening_status in {"eligible", "rejected"}
    )


def _detail_selection_priority(position: Position, *, operation: str) -> tuple[bool, bool, bool, int]:
    """Fetch explicitly routed and legacy rows before ambient catalogue noise."""
    return (
        not (operation == "evidence" and _needs_legacy_revalidation_evidence(position)),
        position.review_state not in {"needs_evidence", "fetch_failed"},
        position.position_type == "other",
        position.id,
    )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _wait_for_cooldown(progress: Progress, until: datetime | None) -> bool:
    """Aspetta senza inviare richieste e resta sensibile al pulsante Stop."""
    while until is not None:
        remaining = (until - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(remaining, _WAIT_CHUNK))
        await progress.check_stop()
        if progress.should_stop:
            return False
    return True


def _apply_detail_screening(
    session: AsyncSession,
    position: Position,
    *,
    full_description: str,
    classified: str,
    promote_after_fetch: bool,
    pipeline_run_id: int | None,
    evidence_route: str,
) -> None:
    """Route fetched text and block only explicit post-fetch contradictions."""
    position.position_type = classified
    if not promote_after_fetch:
        position.review_state = "ready_deep_review"
        position.routing_reason = evidence_route
        return

    guard = screen_enriched_position(
        position.title,
        position.url,
        full_description,
        classified,
        listing_description=position.description,
        deadline=position.deadline,
        deadline_raw=position.deadline_raw,
    )
    if guard.status == "review" and not position.screening_manual:
        reason = f"post_enrich:{guard.reason}"[:256]
        position.screening_status = "review"
        position.screening_reason = reason
        position.screening_source = "router"
        position.screening_decision = "review"
        position.screening_confidence = None
        position.screening_evidence = None
        position.screening_model = None
        position.screening_version = _DETAIL_GUARD_VERSION
        position.review_state = "ready_deep_review"
        position.routing_reason = reason
        position.indexed_at = None
        return
    if guard.status == "rejected" and not position.screening_manual:
        evidence_document = select_evidence_document(
            position.description,
            full_description,
            title=position.title,
            url=position.url,
            deadline=position.deadline,
            deadline_raw=position.deadline_raw,
        )
        quote = detail_rejection_evidence(evidence_document) or position.title
        reason = f"post_enrich:{guard.reason}"[:256]
        position.screening_status = "rejected"
        position.screening_reason = reason
        position.screening_source = "rules"
        position.screening_decision = "rejected"
        position.screening_confidence = 1.0
        position.screening_evidence = json.dumps([quote], ensure_ascii=False)
        position.screening_model = None
        position.screening_version = _DETAIL_GUARD_VERSION
        position.review_state = "resolved"
        position.routing_reason = reason
        append_review_attempt(
            session,
            position_id=position.id,
            pipeline_run_id=pipeline_run_id,
            stage="enrich_guard",
            model=None,
            version=_DETAIL_GUARD_VERSION,
            raw_decision="rejected",
            accepted_status="rejected",
            position_type=classified,
            confidence=1.0,
            evidence=[quote],
            reason=reason,
            tool_attempts=0,
            latency_seconds=0,
            details={"rule_reason": guard.reason},
        )
        return

    evidence_document = select_evidence_document(
        position.description,
        full_description,
        title=position.title,
        url=position.url,
        deadline=position.deadline,
        deadline_raw=position.deadline_raw,
    )
    if getattr(position, "opportunity_kind", "unknown") == "unknown":
        position.opportunity_kind = classify_opportunity_kind_evidence(
            [position.title, evidence_document]
        )
    if position.opportunity_kind in {"unknown", "information"}:
        # A fetched FAQ/application guide may contain PhD and application
        # vocabulary without representing a searchable call. Preserve it for
        # evidence review instead of promoting or deleting it.
        reason = f"post_enrich:opportunity_kind:{position.opportunity_kind}"
        position.screening_status = "review"
        position.screening_reason = reason
        position.screening_source = "router"
        position.screening_decision = "review"
        position.screening_confidence = None
        position.screening_evidence = None
        position.screening_model = None
        position.review_state = "ready_deep_review"
        position.routing_reason = reason
        position.indexed_at = None
        return

    position.screening_status = "eligible"
    if classified != "other":
        position.screening_reason = f"enriched_type:{classified}"
    position.review_state = "resolved"
    position.routing_reason = "details_enriched"


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    """Arricchisce dettagli mancanti; gli errori ordinari restano ritentabili in una run futura."""
    return await _run_details(
        container,
        limit=limit,
        name_like=name_like,
        progress=progress,
        target_status="eligible",
        promote_after_fetch=True,
        operation="enrich",
    )


async def fetch_review_evidence(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    """Scarica il dettaglio degli incerti senza alterarne il verdetto."""
    return await _run_details(
        container,
        limit=limit,
        name_like=name_like,
        progress=progress,
        target_status="review",
        promote_after_fetch=False,
        operation="evidence",
    )


async def _run_details(
    container: Injector,
    *,
    limit: int | None,
    name_like: str | None,
    progress: Progress | None,
    target_status: str,
    promote_after_fetch: bool,
    operation: str,
) -> int:
    progress = progress or Progress()
    session_maker = container.get(async_sessionmaker[AsyncSession])
    checkpoint = await progress.load_checkpoint()
    processed = _checkpoint_int(checkpoint.get("processed", 0))
    enriched = _checkpoint_int(checkpoint.get("enriched", 0))
    raw_deferred = checkpoint.get("deferred_details", {})
    deferred_details = dict(raw_deferred) if isinstance(raw_deferred, dict) else {}
    deferred_processed = _checkpoint_int(checkpoint.get("deferred_processed", 0))
    deferred_total = max(
        _checkpoint_int(checkpoint.get("deferred_total", 0)),
        deferred_processed + len(deferred_details),
    )
    rate_limit_streak = _checkpoint_int(checkpoint.get("euraxess_rate_limit_streak", 0))
    cooldown_until = _parse_timestamp(checkpoint.get("euraxess_cooldown_until"))
    raw_blocked_hosts = checkpoint.get("blocked_detail_hosts", [])
    blocked_hosts = raw_blocked_hosts if isinstance(raw_blocked_hosts, list) else []
    blocked_detail_hosts = {
        str(host).casefold()
        for host in blocked_hosts
        if isinstance(host, str) and host
    }
    remaining = None if limit is None else max(limit - processed, 0)
    today = local_today()
    review_checkpoint = (
        await progress.load_stage_checkpoint("review") if operation == "evidence" else {}
    )
    cohort_scoped = review_checkpoint.get("cohort_complete") is False

    async with session_maker() as session:
        if operation == "evidence":
            swept = await apply_rule_sweep(
                session,
                pipeline_run_id=progress.run_id,
                name_like=name_like,
            )
            if swept:
                await progress.save_checkpoint(rule_sweep_rejected=swept)
        cohort_position_ids: set[int] = set()
        if cohort_scoped and progress.run_id is not None:
            cohort_position_ids = set(
                (
                    await session.execute(
                        select(ReviewAttempt.position_id).where(
                            ReviewAttempt.pipeline_run_id == progress.run_id,
                            ReviewAttempt.stage == "review",
                        )
                    )
                ).scalars().all()
            )
        stmt = (
            select(Position)
            .outerjoin(University, Position.university_id == University.id)
            .where(Position.full_description.is_(None))
            .where(Position.is_active.is_(True))
            .where((Position.deadline.is_(None)) | (Position.deadline >= today))
            .order_by(Position.id)
        )
        if operation == "evidence":
            stmt = stmt.where(Position.review_state != "fetch_unavailable")
        if cohort_scoped:
            stmt = stmt.where(
                or_(
                    Position.id.in_(cohort_position_ids),
                    and_(
                        Position.screening_manual.is_(False),
                        Position.screening_status == "review",
                        Position.review_state.in_(_DURABLE_EVIDENCE_STATES),
                    ),
                    and_(
                        Position.screening_manual.is_(False),
                        Position.screening_source.in_(("llm", "cache")),
                        Position.screening_status.in_(("eligible", "rejected")),
                        Position.screening_version.in_(_LEGACY_REVALIDATION_VERSIONS),
                    ),
                )
            )
        if name_like:
            stmt = stmt.where(University.name.ilike(f"%{name_like}%"))
        candidates = (await session.execute(stmt)).scalars().all()
        selected: list[Position] = []
        status_counts = {"eligible": 0, "review": 0, "rejected": 0}
        screened_at = datetime.now(UTC).replace(tzinfo=None)
        for position in candidates:
            preserve_screening = _preserve_screening_for_detail(
                operation=operation,
                manual=position.screening_manual,
                source=position.screening_source,
                status=position.screening_status,
            ) or (
                position.screening_source == "rules"
                and position.screening_version == RULE_SWEEP_VERSION
                and position.screening_status == "rejected"
            )
            decision = (
                ScreeningDecision(position.screening_status, position.screening_reason or position.screening_source)
                if preserve_screening
                else screen_position(
                    position.title,
                    position.url,
                    position.description,
                    position.position_type,
                )
            )
            if not preserve_screening:
                position.screening_status = decision.status
                position.screening_reason = decision.reason
                position.screening_source = "rules"
                position.screening_decision = decision.status
                position.screening_confidence = 1.0 if decision.status != "review" else None
                position.screening_evidence = None
                position.screening_model = None
                position.screened_at = screened_at
                if decision.status != "eligible":
                    position.indexed_at = None
            legacy_revalidation = (
                operation == "evidence"
                and _needs_legacy_revalidation_evidence(position)
            )
            if decision.status == target_status or legacy_revalidation:
                selected.append(position)
            if decision.status in status_counts:
                status_counts[decision.status] += 1
        await session.commit()
        await progress.save_checkpoint(
            screening_eligible=status_counts["eligible"],
            screening_review=status_counts["review"],
            screening_rejected=status_counts["rejected"],
        )
        selected.sort(
            key=lambda position: _detail_selection_priority(
                position, operation=operation
            )
            if not cohort_scoped
            else (
                position.id not in cohort_position_ids,
                *_detail_selection_priority(position, operation=operation),
            )
        )
        positions = selected if remaining is None else selected[:remaining]
        await progress.begin(len(positions))
        config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, check_robots_txt=True)
        active_ids = {position.id for position in positions}
        deferred_before_filter = len(deferred_details)
        deferred_details = {
            key: value
            for key, value in deferred_details.items()
            if key.isdigit() and int(key) in active_ids
        }
        deferred_processed += deferred_before_filter - len(deferred_details)
        deferred_total = max(deferred_total, deferred_processed + len(deferred_details))
        await progress.save_checkpoint(
            deferred_details=deferred_details,
            deferred_total=deferred_total,
            deferred_processed=deferred_processed,
        )
        ready = deque(positions)
        deferred: deque[Position] = deque()
        ticked: set[int] = set()
        since_deferred = _DEFERRED_INTERLEAVE
        fragment_members: dict[str, list[Position]] = {}
        for candidate in positions:
            if _has_url_fragment(candidate.url):
                fragment_members.setdefault(_fragment_base_url(candidate.url), []).append(candidate)
        fragment_cache: dict[str, dict[int, str | None]] = {}

        async with AsyncWebCrawler() as crawler:
            async def fragment_documents(base_url: str) -> dict[int, str | None]:
                cached = fragment_cache.get(base_url)
                if cached is not None:
                    return cached
                members = fragment_members.get(base_url, [])
                documents: dict[int, str | None]

                async def fetch_fragment_page() -> CrawlResult:
                    result = await crawler.arun(base_url, config=config)
                    if not result.success:
                        raise RuntimeError(result.error_message or f"fragment page fetch failed: {base_url}")
                    if not isinstance(result.html, str) or not result.html.strip():
                        raise RuntimeError(f"empty fragment page HTML: {base_url}")
                    return result

                try:
                    result = await retry_async(
                        progress,
                        f"{operation}:fragment:{sha256(base_url.encode()).hexdigest()[:16]}",
                        fetch_fragment_page,
                        max_attempts=3,
                        base_delay=10,
                        max_delay=60,
                    )
                except RetryInterruptedError:
                    raise
                except Exception as exc:
                    message = str(exc)
                    if "network" in message.casefold():
                        raise RuntimeError(
                            f"fragment {operation} paused at {base_url}; "
                            "resume when connectivity returns"
                        ) from exc
                    print(f"{operation} fragment fetch failed for {base_url}: {message}")
                    documents = {member.id: None for member in members}
                else:
                    documents = {
                        member.id: _extract_fragment_document(
                            result.html,
                            fragment=urlsplit(member.url).fragment,
                            title=member.title,
                        )
                        for member in members
                    }
                fragment_cache[base_url] = documents
                return documents

            while ready or deferred:
                if progress.should_stop:
                    break

                cooldown_active = (
                    cooldown_until is not None and cooldown_until > datetime.now(UTC)
                )
                take_deferred = bool(
                    deferred
                    and not cooldown_active
                    and (not ready or since_deferred >= _DEFERRED_INTERLEAVE)
                )
                if take_deferred:
                    position = deferred.popleft()
                    since_deferred = 0
                elif ready:
                    position = ready.popleft()
                    if _is_euraxess_url(position.url) and cooldown_active:
                        deferred.append(position)
                        continue
                    since_deferred += 1
                else:
                    if not await _wait_for_cooldown(progress, cooldown_until):
                        break
                    position = deferred.popleft()
                    since_deferred = 0

                position_id = position.id
                position_url = position.url
                position_title = position.title

                if position_id not in ticked:
                    await progress.tick(position_title)
                    ticked.add(position_id)

                if _has_url_fragment(position_url):
                    inline_text = position.description or position_title
                    try:
                        extracted = (
                            await fragment_documents(_fragment_base_url(position_url))
                        ).get(position_id)
                    except RetryInterruptedError:
                        break
                    evidence_text = (
                        extracted
                        if extracted and _has_sufficient_inline_evidence(extracted)
                        else inline_text
                        if _has_attributable_fragment_evidence(inline_text, position.title)
                        else None
                    )
                    if evidence_text is None:
                        if promote_after_fetch:
                            position.review_state = "resolved"
                            position.routing_reason = "enrich:fragment_evidence_unavailable"
                        else:
                            position.review_state = "fetch_unavailable"
                            position.routing_reason = "evidence:fragment_unattributable"
                        if promote_after_fetch:
                            position.screened_at = datetime.now(UTC).replace(tzinfo=None)
                        await session.commit()
                        processed += 1
                        if deferred_details.pop(str(position_id), None) is not None:
                            deferred_processed += 1
                        await progress.save_checkpoint(
                            processed=processed,
                            enriched=enriched,
                            last_position_id=position_id,
                            deferred_details=deferred_details,
                            deferred_total=deferred_total,
                            deferred_processed=deferred_processed,
                        )
                        continue
                    position.full_description = evidence_text
                    _apply_extracted_detail_metadata(position, evidence_text)
                    classified = classify_position(
                        position.title,
                        evidence_text,
                        position.position_type,
                    )
                    _apply_detail_screening(
                        session,
                        position,
                        full_description=evidence_text,
                        classified=classified,
                        promote_after_fetch=promote_after_fetch,
                        pipeline_run_id=progress.run_id,
                        evidence_route=(
                            "evidence:fragment_dom"
                            if extracted
                            else "evidence:inline_description"
                        ),
                    )
                    if promote_after_fetch:
                        position.screened_at = datetime.now(UTC).replace(tzinfo=None)
                    position.details_scraped_at = datetime.now(UTC).replace(tzinfo=None)
                    position.indexed_at = None
                    await session.commit()
                    enriched += 1
                    processed += 1
                    if deferred_details.pop(str(position_id), None) is not None:
                        deferred_processed += 1
                    await progress.save_checkpoint(
                        processed=processed,
                        enriched=enriched,
                        last_position_id=position_id,
                        deferred_details=deferred_details,
                        deferred_total=deferred_total,
                        deferred_processed=deferred_processed,
                    )
                    continue

                is_euraxess = _is_euraxess_url(position_url)
                is_direct_document = _looks_like_direct_document(position_url)
                position_host = (urlsplit(position_url).hostname or "").casefold()
                if not _is_supported_detail_url(position_url):
                    if operation == "evidence":
                        position.review_state = "fetch_unavailable"
                        position.routing_reason = "evidence:unsupported_url_scheme"
                        await session.commit()
                    if deferred_details.pop(str(position_id), None) is not None:
                        deferred_processed += 1
                    processed += 1
                    await progress.save_checkpoint(
                        processed=processed,
                        enriched=enriched,
                        last_position_id=position_id,
                        deferred_details=deferred_details,
                        deferred_total=deferred_total,
                        deferred_processed=deferred_processed,
                    )
                    continue
                if _looks_like_unsupported_asset(position_url):
                    if operation == "evidence":
                        position.review_state = "fetch_unavailable"
                        position.routing_reason = "evidence:unsupported_asset"
                        await session.commit()
                    if deferred_details.pop(str(position_id), None) is not None:
                        deferred_processed += 1
                    processed += 1
                    await progress.save_checkpoint(
                        processed=processed,
                        enriched=enriched,
                        last_position_id=position_id,
                        deferred_details=deferred_details,
                        deferred_total=deferred_total,
                        deferred_processed=deferred_processed,
                    )
                    continue
                if position_host in blocked_detail_hosts:
                    if operation == "evidence":
                        position.review_state = "fetch_failed"
                        position.routing_reason = "evidence:host_access_blocked"
                        await session.commit()
                    if deferred_details.pop(str(position_id), None) is not None:
                        deferred_processed += 1
                    processed += 1
                    await progress.save_checkpoint(
                        processed=processed,
                        enriched=enriched,
                        last_position_id=position_id,
                        deferred_details=deferred_details,
                        deferred_total=deferred_total,
                        deferred_processed=deferred_processed,
                    )
                    continue

                async def fetch(
                    url: str = position_url,
                    direct_document: bool = is_direct_document,
                ) -> str:
                    if direct_document:
                        return await _fetch_direct_document(url)
                    return await _crawl_detail_document(crawler, url, config)

                try:
                    # Version the key when retry semantics change: an exhausted
                    # Crawl4AI retry must not prevent a newly supported PDF from
                    # being attempted after Resume.
                    retry_key = (
                        f"{operation}:fetch-{_DETAIL_FETCH_RETRY_VERSION}:{position_id}"
                    )
                    result = await retry_async(
                        progress,
                        retry_key,
                        fetch,
                        # EURAXESS non deve trattenere il worker: al primo 429
                        # passa nella coda e lascia spazio agli altri host.
                        max_attempts=1 if is_euraxess else 3,
                        base_delay=10,
                        max_delay=60,
                        non_retryable=(
                            _UnsupportedDirectDocumentError,
                            _AccessBlockedError,
                        ),
                    )
                except RetryInterruptedError:
                    break
                except Exception as exc:
                    message = str(exc)
                    print(f"{operation} fetch failed for {position_url}: {message}")
                    if isinstance(exc, _AccessBlockedError):
                        if position_host:
                            blocked_detail_hosts.add(position_host)
                            await progress.save_checkpoint(
                                blocked_detail_hosts=sorted(blocked_detail_hosts)
                            )
                        if operation == "evidence":
                            position.review_state = "fetch_failed"
                            position.routing_reason = "evidence:access_blocked"
                            await session.commit()
                        if deferred_details.pop(str(position_id), None) is not None:
                            deferred_processed += 1
                        processed += 1
                        await progress.save_checkpoint(
                            processed=processed,
                            enriched=enriched,
                            last_position_id=position_id,
                            deferred_details=deferred_details,
                            deferred_total=deferred_total,
                            deferred_processed=deferred_processed,
                        )
                        continue
                    if is_euraxess and _is_rate_limit(exc):
                        # Il vecchio retry della run #21 aveva già esaurito tre
                        # tentativi: pulirlo permette alla coda di riprovare.
                        await clear_retry(progress, retry_key)
                        rate_limit_streak += 1
                        cooldown = _cooldown_seconds(rate_limit_streak)
                        cooldown_until = datetime.now(UTC) + timedelta(seconds=cooldown)
                        deferred_key = str(position_id)
                        if deferred_key not in deferred_details:
                            deferred_total += 1
                        deferred_details[deferred_key] = {
                            "url": position_url,
                            "reason": message[:1000],
                            "attempts": rate_limit_streak,
                            "retry_at": cooldown_until.isoformat(),
                        }
                        await progress.save_checkpoint(
                            deferred_details=deferred_details,
                            deferred_total=deferred_total,
                            deferred_processed=deferred_processed,
                            euraxess_rate_limit_streak=rate_limit_streak,
                            euraxess_cooldown_until=cooldown_until.isoformat(),
                        )
                        deferred.append(position)
                        print(
                            f"{operation}: queued EURAXESS position {position_id}; "
                            f"retry in {int(cooldown)}s"
                        )
                        continue
                    if "network" in message.casefold():
                        await clear_retry(progress, retry_key)
                        if operation == "evidence":
                            position.review_state = "fetch_failed"
                            position.routing_reason = "evidence:network_unavailable"
                            await session.commit()
                        raise RuntimeError(
                            f"detail {operation} paused at {position_url}; "
                            "resume when connectivity returns"
                        ) from exc
                    if operation == "evidence":
                        position.review_state = "fetch_failed"
                        position.routing_reason = f"evidence:fetch_failed:{message}"[:256]
                        await session.commit()
                    if deferred_details.pop(str(position_id), None) is not None:
                        deferred_processed += 1
                    processed += 1
                    await progress.save_checkpoint(
                        processed=processed,
                        enriched=enriched,
                        last_position_id=position_id,
                        deferred_details=deferred_details,
                        deferred_total=deferred_total,
                        deferred_processed=deferred_processed,
                    )
                    continue

                try:
                    async with session.begin_nested():
                        full_description = result
                        _apply_extracted_detail_metadata(position, full_description)
                        position.full_description = full_description
                        classified = classify_position(
                            position_title,
                            full_description,
                            position.position_type,
                        )
                        _apply_detail_screening(
                            session,
                            position,
                            full_description=full_description,
                            classified=classified,
                            promote_after_fetch=promote_after_fetch,
                            pipeline_run_id=progress.run_id,
                            evidence_route=(
                                "evidence:pdf_fetched"
                                if is_direct_document
                                else "evidence:detail_fetched"
                            ),
                        )
                        if promote_after_fetch:
                            position.screened_at = datetime.now(UTC).replace(tzinfo=None)
                        position.details_scraped_at = datetime.now(UTC).replace(tzinfo=None)
                        position.indexed_at = None
                except Exception as exc:
                    print(f"{operation} parse failed for {position_url}: {exc}")
                else:
                    # Un errore di commit indica un problema DB globale: non va
                    # inghiottito come se fosse una singola pagina malformata.
                    await session.commit()
                    enriched += 1
                if is_euraxess:
                    if deferred_details.pop(str(position_id), None) is not None:
                        deferred_processed += 1
                    rate_limit_streak = 0
                    cooldown_until = None
                processed += 1
                await progress.save_checkpoint(
                    processed=processed,
                    enriched=enriched,
                    last_position_id=position_id,
                    deferred_details=deferred_details,
                    deferred_total=deferred_total,
                    deferred_processed=deferred_processed,
                    euraxess_rate_limit_streak=rate_limit_streak,
                    euraxess_cooldown_until=(
                        None if cooldown_until is None else cooldown_until.isoformat()
                    ),
                )
                await asyncio.sleep(_EURAXESS_DETAIL_DELAY if is_euraxess else 1)
    print(f"{operation}: {enriched} full detail pages saved")
    return enriched
