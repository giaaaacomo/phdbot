"""Quality gate conservativo tra scrape e review.

Il gate riconosce soltanto artefatti tecnici ad alta precisione.  Non elimina
righe: ``quarantine`` significa che l'estrazione va ispezionata o ripetuta,
mentre ``reject`` e' riservato a link di navigazione inequivocabili.  Le
funzioni pure in questo modulo rendono inoltre possibile valutare la salute di
ogni ``ListingPage`` prima di decidere se rigenerarne lo schema.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from itertools import groupby
from typing import TypedDict
from urllib.parse import unquote, urlsplit

from injector import Injector
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.schema_quality import schema_quality_issues
from phd_searcher.pipeline.source_family import SOURCE_FAMILY_VERSION, source_family_keys

QUALITY_GATE_VERSION = "quality-gate-v2"


class QualityDisposition(StrEnum):
    """Esito per una singola riga, distinto dallo stato della fonte."""

    PASS = "pass"
    REJECT = "reject"
    QUARANTINE = "quarantine"


class QualityReason(StrEnum):
    """Reason code stabile e adatto a telemetria/UI."""

    INVALID_URL_SCHEME = "invalid_url_scheme"
    INVALID_URL_HOST = "invalid_url_host"
    INVALID_URL_SYNTAX = "invalid_url_syntax"
    URL_HTML_MARKUP = "url_html_markup"
    ASSET_URL = "asset_url"
    EMPTY_TITLE = "empty_title"
    TITLE_HTML_MARKUP = "title_html_markup"
    TITLE_SCRIPT = "title_script"
    TITLE_ASSET_NAME = "title_asset_name"
    TITLE_CONTROL_CHARS = "title_control_chars"
    TITLE_ABSURD = "title_absurd"
    NAVIGATION_TITLE = "navigation_title"
    CONTENT_SECTION_MISMATCH = "content_section_mismatch"
    EDITORIAL_ARCHIVE_ITEM = "editorial_archive_item"
    SOURCE_HEALTH_QUARANTINE = "source_health_quarantine"


class ListingHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    QUARANTINE = "quarantine"


class _UrlFamilyMetric(TypedDict):
    total: int
    passed: int
    rejected: int
    quarantined: int
    suspected: int
    issue_ratio: float
    reason_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class QualityDecision:
    disposition: QualityDisposition
    reasons: tuple[QualityReason, ...] = ()

    @property
    def primary_reason(self) -> QualityReason | None:
        return self.reasons[0] if self.reasons else None


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    position_id: int
    listing_page_id: int | None
    listing_url: str | None
    decision: QualityDecision
    # Segnali deboli usati soltanto in aggregato. Un singolo articolo/pagina
    # formativa non viene mai rifiutato o messo in quarantena da queste euristiche.
    source_signals: tuple[QualityReason, ...] = ()
    # Chiavi strutturali ordinate, usate soltanto per telemetria shadow. Non sono
    # evidenza candidate-specific e non producono verdetti in questa versione.
    url_family_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ListingHealth:
    """Aggregato in-memory; non modifica automaticamente ``ListingPage``."""

    listing_page_id: int | None
    listing_url: str | None
    status: ListingHealthStatus
    total: int
    passed: int
    rejected: int
    quarantined: int
    suspected: int
    reason_counts: dict[str, int]
    url_family_metrics: dict[str, object] = field(default_factory=dict)

    @property
    def issue_ratio(self) -> float:
        return (
            (self.rejected + self.quarantined + self.suspected) / self.total
            if self.total
            else 0.0
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "listing_page_id": self.listing_page_id,
            "listing_url": self.listing_url,
            "status": self.status.value,
            "total": self.total,
            "passed": self.passed,
            "rejected": self.rejected,
            "quarantined": self.quarantined,
            "suspected": self.suspected,
            "issue_ratio": self.issue_ratio,
            "reason_counts": dict(self.reason_counts),
            "url_family_metrics": dict(self.url_family_metrics),
        }


_SPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML_TAG_RE = re.compile(
    r"<\s*/?\s*(?:!doctype|html|head|body|meta|link|script|style|noscript|iframe|svg|path|"
    r"div|span|section|article|nav|header|footer|form|input|button|a|img|picture|source|"
    r"table|tr|td|ul|ol|li|h[1-6]|p|br)\b[^>]*>",
    re.IGNORECASE,
)
_SCRIPT_RE = re.compile(
    r"(?:"
    r"\b(?:window|document|navigator|location)\s*\.\s*[A-Za-z_$]|"
    r"\b(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=|"
    r"\bfunction\s+(?:[A-Za-z_$][\w$]*\s*)?\([^)]*\)\s*\{|"
    r"\b(?:addEventListener|querySelector|getElementById)\s*\(|"
    r"[\"']?@context[\"']?\s*:\s*[\"']https?://schema\.org|"
    r"\b(?:src|href|rel|content)\s*=\s*[\"'][^\"']+[\"']|"
    r"(?:^|[;}])\s*[.#]?[A-Za-z][\w -]{0,40}\s*\{\s*[\w-]+\s*:"
    r")",
    re.IGNORECASE,
)
_ASSET_SUFFIX_RE = re.compile(
    r"\.(?:css|m?js|map|png|jpe?g|gif|webp|avif|svg|ico|bmp|woff2?|ttf|otf|eot)(?:$|[?#])",
    re.IGNORECASE,
)
_ASSET_TITLE_RE = re.compile(
    r"^(?:[\w@.+~-]+/)*[\w@.+~-]+\.(?:css|m?js|map|png|jpe?g|gif|webp|avif|svg|ico|woff2?|ttf|otf|eot)$",
    re.IGNORECASE,
)
_REPEATED_PUNCTUATION_RE = re.compile(r"([^\w\s])\1{7,}")
_URL_AS_TITLE_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_OPPORTUNITY_SIGNAL_RE = re.compile(
    r"(?:"
    r"\bvacanc(?:y|ies)\b|\bjob(?:s)?\b|\bopen(?:ing)? positions?\b|"
    r"\bdoctoral (?:candidate|researcher|student|position|vacanc(?:y|ies))\b|"
    r"\bph\.?d\b|\bpost-?doc(?:toral)?\b|\b(?:assistant|student)ships?\b|"
    r"\b(?:fellow|intern|scholar)ships?\b|\bcall for (?:applications|candidates)\b|"
    r"\brecruit(?:ing|ment)\b|\bstellenangebote?\b|\bstellenausschreibung\w*\b|"
    r"\bdoktorand\w*\b|\bpromotionsstelle\w*\b|\bhilfskraft\b|\bprofessur\w*\b|"
    r"\bbewerbung\w*\b"
    r")",
    re.IGNORECASE,
)
_EDITORIAL_SEGMENTS = frozenset(
    {
        "blog",
        "event",
        "events",
        "news",
        "news-events",
        "newsroom",
        "press",
        "press-releases",
    }
)
_STUDY_SEGMENTS = frozenset(
    {
        "admission",
        "admissions",
        "course",
        "courses",
        "degree-programmes",
        "degree-programs",
        "programme",
        "programmes",
        "program",
        "programs",
        "studies",
        "studium",
        "studierende",
        "study",
    }
)

_NAVIGATION_TITLES = frozenset(
    {
        "about",
        "about us",
        "apply",
        "back",
        "careers",
        "contact",
        "contact us",
        "cookie policy",
        "cookie settings",
        "download",
        "events",
        "faq",
        "home",
        "jobs",
        "learn more",
        "login",
        "menu",
        "more",
        "news",
        "next",
        "open menu",
        "previous",
        "privacy",
        "privacy policy",
        "read more",
        "research",
        "search",
        "see all",
        "show more",
        "sign in",
        "skip to content",
        "study",
        "terms and conditions",
        "view all",
    }
)


def _clean_title(title: str) -> str:
    return _SPACE_RE.sub(" ", title).strip()


def _url_reasons(url: str) -> list[QualityReason]:
    value = url.strip()
    try:
        parsed = urlsplit(value)
        # Accedere a hostname/port completa la validazione sintattica di IPv6 e porta.
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return [QualityReason.INVALID_URL_SYNTAX]

    reasons: list[QualityReason] = []
    if parsed.scheme.casefold() not in {"http", "https"}:
        reasons.append(QualityReason.INVALID_URL_SCHEME)
    if not hostname or any(character.isspace() for character in hostname):
        reasons.append(QualityReason.INVALID_URL_HOST)
    if _ASSET_SUFFIX_RE.search(unquote(parsed.path)):
        reasons.append(QualityReason.ASSET_URL)
    decoded_url = html.unescape(unquote(value))
    if _HTML_TAG_RE.search(decoded_url):
        reasons.append(QualityReason.URL_HTML_MARKUP)
    return reasons


def _title_reasons(title: str) -> tuple[list[QualityReason], bool]:
    """Ritorna reason tecnici e se il titolo e' una navigazione certa."""
    clean = _clean_title(title)
    decoded = html.unescape(clean)
    reasons: list[QualityReason] = []

    if not clean:
        reasons.append(QualityReason.EMPTY_TITLE)
    if _CONTROL_RE.search(title):
        reasons.append(QualityReason.TITLE_CONTROL_CHARS)
    if _HTML_TAG_RE.search(decoded):
        reasons.append(QualityReason.TITLE_HTML_MARKUP)
    if _SCRIPT_RE.search(decoded):
        reasons.append(QualityReason.TITLE_SCRIPT)
    if _ASSET_TITLE_RE.fullmatch(decoded):
        reasons.append(QualityReason.TITLE_ASSET_NAME)

    visible = [character for character in decoded if not character.isspace()]
    alphanumeric = sum(character.isalnum() for character in visible)
    punctuation_noise = bool(_REPEATED_PUNCTUATION_RE.search(decoded))
    looks_like_url = bool(_URL_AS_TITLE_RE.fullmatch(decoded))
    if (len(visible) >= 12 and alphanumeric / len(visible) < 0.2) or punctuation_noise or looks_like_url:
        reasons.append(QualityReason.TITLE_ABSURD)

    folded = decoded.casefold().strip(" .:;|/-_\u00a0")
    return list(dict.fromkeys(reasons)), folded in _NAVIGATION_TITLES


def inspect_candidate(*, title: str, url: str, description: str = "") -> QualityDecision:
    """Classifica un candidato senza rete e senza modificare dati.

    ``description`` fa parte dell'API per consentire futuri controlli corroboranti,
    ma non viene usata per bocciare: markup HTML nelle descrizioni e' comune e non
    dimostra da solo che l'item sia un artefatto.
    """
    del description
    reasons = _url_reasons(url)
    title_reasons, is_navigation = _title_reasons(title)
    reasons.extend(title_reasons)
    unique_reasons = tuple(dict.fromkeys(reasons))
    if unique_reasons:
        return QualityDecision(QualityDisposition.QUARANTINE, unique_reasons)
    if is_navigation:
        return QualityDecision(QualityDisposition.REJECT, (QualityReason.NAVIGATION_TITLE,))
    return QualityDecision(QualityDisposition.PASS)


def _content_groups(url: str | None) -> frozenset[str]:
    if not url:
        return frozenset()
    try:
        segments = {
            segment.casefold().replace("_", "-")
            for segment in unquote(urlsplit(url).path).split("/")
            if segment
        }
    except ValueError:
        return frozenset()
    groups: set[str] = set()
    if segments & _EDITORIAL_SEGMENTS:
        groups.add("editorial")
    if segments & _STUDY_SEGMENTS:
        groups.add("study")
    return frozenset(groups)


def _source_signals(*, title: str, url: str, listing_url: str | None) -> tuple[QualityReason, ...]:
    """Segnali non conclusivi che acquistano significato solo su una fonte intera."""
    # Un titolo o URL esplicitamente occupazionale resta fuori dalle euristiche
    # editoriali/formative: una vacancy puo' legittimamente essere pubblicata come news.
    if _OPPORTUNITY_SIGNAL_RE.search(f"{title} {url}"):
        return ()
    candidate_groups = _content_groups(url)
    if not candidate_groups:
        return ()
    listing_groups = _content_groups(listing_url)
    signals: list[QualityReason] = []
    if "editorial" in candidate_groups:
        signals.append(QualityReason.EDITORIAL_ARCHIVE_ITEM)
    if not candidate_groups.intersection(listing_groups):
        signals.append(QualityReason.CONTENT_SECTION_MISMATCH)
    return tuple(signals)


def assess_candidate(
    *,
    position_id: int,
    listing_page_id: int | None,
    listing_url: str | None,
    title: str,
    url: str,
    description: str = "",
) -> CandidateAssessment:
    return CandidateAssessment(
        position_id=position_id,
        listing_page_id=listing_page_id,
        listing_url=listing_url,
        decision=inspect_candidate(title=title, url=url, description=description),
        source_signals=_source_signals(title=title, url=url, listing_url=listing_url),
        url_family_keys=source_family_keys(
            url,
            listing_url=listing_url,
            listing_page_id=listing_page_id,
        ),
    )


def summarize_url_families(
    assessments: Iterable[CandidateAssessment],
    *,
    maximum_families: int = 20,
) -> dict[str, object]:
    """Describe repeated route families without changing any candidate verdict.

    Candidates can contribute to a specific template and a weaker parent route.
    The overlap is intentional and explicit in the key. Only repeated families
    are persisted, with a hard cap that keeps ``ListingPage.quality_metrics``
    bounded even on very large aggregators.
    """
    if maximum_families < 0:
        raise ValueError("maximum_families must be non-negative")
    rows = list(assessments)
    grouped: dict[str, list[CandidateAssessment]] = {}
    for row in rows:
        for key in row.url_family_keys:
            grouped.setdefault(key, []).append(row)

    repeated: list[tuple[str, _UrlFamilyMetric]] = []
    for key, family_rows in grouped.items():
        if len(family_rows) < 2:
            continue
        dispositions = Counter(row.decision.disposition for row in family_rows)
        signals = Counter(reason.value for row in family_rows for reason in row.source_signals)
        suspected = sum(
            bool(row.source_signals) and row.decision.disposition == QualityDisposition.PASS
            for row in family_rows
        )
        issues = (
            dispositions[QualityDisposition.REJECT]
            + dispositions[QualityDisposition.QUARANTINE]
            + suspected
        )
        repeated.append(
            (
                key,
                {
                    "total": len(family_rows),
                    "passed": dispositions[QualityDisposition.PASS],
                    "rejected": dispositions[QualityDisposition.REJECT],
                    "quarantined": dispositions[QualityDisposition.QUARANTINE],
                    "suspected": suspected,
                    "issue_ratio": issues / len(family_rows),
                    "reason_counts": dict(sorted(signals.items())),
                },
            )
        )
    repeated.sort(
        key=lambda item: (
            -item[1]["suspected"],
            -item[1]["quarantined"],
            -item[1]["rejected"],
            -item[1]["total"],
            item[0],
        )
    )
    selected = repeated[:maximum_families]
    return {
        "version": SOURCE_FAMILY_VERSION,
        "candidate_count": len(rows),
        "distinct_keys": len(grouped),
        "repeated_keys": len(repeated),
        "reported_keys": len(selected),
        "truncated": len(repeated) > len(selected),
        "families": dict(selected),
    }


def summarize_listing(
    assessments: Iterable[CandidateAssessment],
    *,
    source_reasons: Iterable[str] = (),
) -> ListingHealth:
    """Calcola lo stato di una fonte con soglie conservative e spiegabili.

    Una fonte viene messa in quarantena se lo schema e' strutturalmente
    pericoloso, se almeno cinque item e il 20% sono artefatti tecnici, oppure se
    almeno dieci item e meta' dell'estrazione sono artefatti/navigazione. I
    segnali editoriali o di cambio sezione richiedono inoltre numerosita' e
    rapporto elevati. Un singolo segnale degrada la salute ma non ferma la fonte.
    """
    rows = list(assessments)
    if not rows:
        raise ValueError("summarize_listing requires at least one assessment")
    listing_ids = {row.listing_page_id for row in rows}
    if len(listing_ids) != 1:
        raise ValueError("assessments from different listing pages cannot be summarized together")

    disposition_counts = Counter(row.decision.disposition for row in rows)
    reason_counts = Counter(reason.value for row in rows for reason in row.decision.reasons)
    signal_counts = Counter(reason.value for row in rows for reason in row.source_signals)
    reason_counts.update(signal_counts)
    structural_reasons = tuple(dict.fromkeys(source_reasons))
    reason_counts.update(structural_reasons)
    total = len(rows)
    quarantined = disposition_counts[QualityDisposition.QUARANTINE]
    rejected = disposition_counts[QualityDisposition.REJECT]
    passed = disposition_counts[QualityDisposition.PASS]
    suspected = sum(
        bool(row.source_signals) and row.decision.disposition == QualityDisposition.PASS
        for row in rows
    )
    technical_ratio = quarantined / total
    issue_ratio = (quarantined + rejected) / total
    source_quarantine = (quarantined >= 5 and technical_ratio >= 0.20) or (
        quarantined + rejected >= 10 and issue_ratio >= 0.50
    )
    source_quarantine = (
        source_quarantine
        or bool(structural_reasons)
        or (
            signal_counts[QualityReason.CONTENT_SECTION_MISMATCH.value] >= 10
            and signal_counts[QualityReason.CONTENT_SECTION_MISMATCH.value] / total >= 0.50
        )
        or (
            signal_counts[QualityReason.EDITORIAL_ARCHIVE_ITEM.value] >= 20
            and signal_counts[QualityReason.EDITORIAL_ARCHIVE_ITEM.value] / total >= 0.80
        )
    )
    status = (
        ListingHealthStatus.QUARANTINE
        if source_quarantine
        else ListingHealthStatus.DEGRADED
        if quarantined or rejected or suspected
        else ListingHealthStatus.HEALTHY
    )
    return ListingHealth(
        listing_page_id=rows[0].listing_page_id,
        listing_url=rows[0].listing_url,
        status=status,
        total=total,
        passed=passed,
        rejected=rejected,
        quarantined=quarantined,
        suspected=suspected,
        reason_counts=dict(sorted(reason_counts.items())),
        url_family_metrics=summarize_url_families(rows),
    )


def aggregate_listing_health(assessments: Iterable[CandidateAssessment]) -> list[ListingHealth]:
    """Raggruppa valutazioni eterogenee per ListingPage, in ordine stabile."""
    rows = sorted(
        assessments,
        key=lambda row: (
            row.listing_page_id is None,
            row.listing_page_id if row.listing_page_id is not None else row.position_id,
        ),
    )
    result: list[ListingHealth] = []
    for _key, group in groupby(rows, key=lambda row: row.listing_page_id):
        result.append(summarize_listing(group))
    return result


def _apply_decision(position: Position, decision: QualityDecision, *, now: datetime) -> None:
    """Applica soltanto esiti conclusivi; PASS non sovrascrive review precedenti."""
    if position.screening_manual or decision.disposition == QualityDisposition.PASS:
        return
    status = "quarantine" if decision.disposition == QualityDisposition.QUARANTINE else "rejected"
    reason_codes = [reason.value for reason in decision.reasons]
    position.screening_status = status
    position.screening_reason = f"quality_gate:{status}:{','.join(reason_codes)}"[:256]
    position.screening_source = "quality_gate"
    position.screening_decision = status
    position.screening_confidence = 1.0
    position.screening_evidence = json.dumps([f"reason_code:{reason}" for reason in reason_codes])
    position.screening_model = None
    position.screening_version = QUALITY_GATE_VERSION
    position.screened_at = now
    position.review_state = "source_broken" if status == "quarantine" else "resolved"
    position.routing_reason = position.screening_reason
    position.indexed_at = None


def _recover_clean_candidate(
    position: Position,
    decision: QualityDecision,
    *,
    now: datetime,
) -> bool:
    """Rimette in triage un vecchio esito del gate solo su una fonte pulita.

    Il chiamante deve invocare questa funzione esclusivamente quando l'intera
    ``ListingPage`` e' tornata ``healthy``. Il recupero e' intenzionalmente
    conservativo: non promuove direttamente la posizione a ``eligible``.
    """
    if (
        position.screening_manual
        or decision.disposition != QualityDisposition.PASS
        or position.screening_source != "quality_gate"
        or position.screening_status not in {"quarantine", "rejected"}
    ):
        return False

    position.screening_status = "pending"
    position.screening_reason = "quality_gate:recovered_clean_source"
    position.screening_source = "rules"
    position.screening_decision = None
    position.screening_confidence = None
    position.screening_evidence = None
    position.screening_model = None
    position.screening_version = None
    position.screened_at = now
    position.review_state = "untriaged"
    position.routing_reason = "quality_gate:source_recovered"
    position.indexed_at = None
    return True


def _apply_listing_health(listing_page: ListingPage, health: ListingHealth, *, now: datetime) -> None:
    """Persiste l'aggregato e richiede un nuovo schema per fonti in quarantena."""
    listing_page.quality_status = health.status.value
    listing_page.quality_metrics = health.as_dict()
    listing_page.quality_checked_at = now
    if health.status == ListingHealthStatus.HEALTHY:
        listing_page.quality_reason = None
        return
    ranked_reasons = sorted(health.reason_counts.items(), key=lambda item: (-item[1], item[0]))
    reason_summary = ",".join(f"{reason}:{count}" for reason, count in ranked_reasons[:5])
    listing_page.quality_reason = f"quality_gate:{health.status.value}:{reason_summary}"[:256]
    if health.status == ListingHealthStatus.QUARANTINE:
        # Lo schema non viene cancellato: ``stale`` fa si' che la fase schema
        # canonica successiva lo rigeneri, lasciando intatti dati e audit.
        listing_page.schema_status = "stale"


def _group_key(row: tuple[Position, ListingPage | None, University | None]) -> str:
    position, listing_page, _university = row
    return f"listing:{listing_page.id}" if listing_page is not None else f"orphan:{position.id}"


def _stop_requested(progress: Progress) -> bool:
    """Legge il segnale mutabile senza affidarsi al narrowing tra chiamate async."""
    return progress.should_stop


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    """Valuta fonti post-scrape; ``limit`` limita le listing source, non cancella dati.

    Gli aggregati e i relativi reason code vengono persistiti sulla
    ``ListingPage``. Le fonti anomale sono messe in quarantena in modo
    reversibile: i dati estratti non vengono cancellati né disattivati.
    """
    progress = progress or Progress()
    session_maker = container.get(async_sessionmaker[AsyncSession])
    checkpoint = await progress.load_checkpoint()
    raw_completed = checkpoint.get("completed_listing_keys", [])
    completed = {str(value) for value in raw_completed} if isinstance(raw_completed, list) else set()
    raw_processed = checkpoint.get("processed_positions", 0)
    processed = int(raw_processed) if isinstance(raw_processed, int | str) and str(raw_processed).isdigit() else 0
    raw_reports = checkpoint.get("listing_health", {})
    reports = dict(raw_reports) if isinstance(raw_reports, dict) else {}

    async with session_maker() as session:
        stmt = (
            select(Position, ListingPage, University)
            .outerjoin(ListingPage, Position.listing_page_id == ListingPage.id)
            .outerjoin(University, Position.university_id == University.id)
            .where(
                Position.is_active.is_(True),
                Position.screening_manual.is_(False),
                # Rivaluta anche quarantene e reject prodotti da questo gate:
                # possono essere recuperati dopo una nuova estrazione pulita.
                # Reject di regole/LLM e decisioni manuali restano esclusi.
                or_(
                    Position.screening_status.in_(("pending", "review", "eligible", "quarantine")),
                    and_(
                        Position.screening_status == "rejected",
                        Position.screening_source == "quality_gate",
                    ),
                ),
            )
            .order_by(Position.listing_page_id.asc().nulls_last(), Position.id)
        )
        if name_like:
            pattern = f"%{name_like}%"
            stmt = stmt.where(
                or_(
                    University.name.ilike(pattern),
                    Position.institution_name.ilike(pattern),
                    ListingPage.url.ilike(pattern),
                )
            )
        rows: list[tuple[Position, ListingPage | None, University | None]] = [
            (position, listing_page, university)
            for position, listing_page, university in (await session.execute(stmt)).all()
        ]
        grouped = [(key, list(group)) for key, group in groupby(rows, key=_group_key) if key not in completed]
        if limit is not None:
            grouped = grouped[:limit]
        await progress.begin(len(grouped))

        for key, source_rows in grouped:
            if _stop_requested(progress):
                break
            first_position, listing_page, university = source_rows[0]
            label = (
                university.name
                if university is not None
                else first_position.institution_name
                or (listing_page.url if listing_page is not None else first_position.url)
            )
            await progress.tick(label)
            if _stop_requested(progress):
                break

            now = datetime.now(UTC).replace(tzinfo=None)
            assessments: list[CandidateAssessment] = []
            for position, page, _university in source_rows:
                assessment = assess_candidate(
                    position_id=position.id,
                    listing_page_id=page.id if page is not None else None,
                    listing_url=page.url if page is not None else None,
                    title=position.title,
                    url=position.url,
                    description=position.full_description or position.description,
                )
                assessments.append(assessment)
                _apply_decision(position, assessment.decision, now=now)
            health = summarize_listing(
                assessments,
                source_reasons=(
                    schema_quality_issues(listing_page.extraction_schema)
                    if listing_page is not None
                    else ()
                ),
            )
            if health.status == ListingHealthStatus.QUARANTINE:
                source_decision = QualityDecision(
                    QualityDisposition.QUARANTINE,
                    (QualityReason.SOURCE_HEALTH_QUARANTINE,),
                )
                for (position, _page, _university), assessment in zip(source_rows, assessments, strict=True):
                    if assessment.decision.disposition == QualityDisposition.PASS:
                        _apply_decision(position, source_decision, now=now)
            elif health.status == ListingHealthStatus.HEALTHY:
                for (position, _page, _university), assessment in zip(
                    source_rows,
                    assessments,
                    strict=True,
                ):
                    _recover_clean_candidate(position, assessment.decision, now=now)
            if listing_page is not None:
                _apply_listing_health(listing_page, health, now=now)
            await session.commit()

            reports[key] = health.as_dict()
            processed += len(source_rows)
            completed.add(key)
            await progress.save_checkpoint(
                completed_listing_keys=sorted(completed),
                processed_positions=processed,
                listing_health=reports,
            )
            await progress.check_stop()
            if _stop_requested(progress):
                break
    return processed
