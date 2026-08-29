"""Stadio 5: embedding (litellm) + upsert in Qdrant; rimozione bandi scaduti."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from injector import Injector
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    SetPayload,
    SetPayloadOperation,
    VectorParams,
)
from sqlalchemy import and_, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select

from phd_searcher.clock import local_today
from phd_searcher.config.qdrant import QdrantConfig
from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.position_feedback import PositionFeedback
from phd_searcher.database.models.university import University
from phd_searcher.engine.model_helper import ModelHelper
from phd_searcher.engine.search_contract import validate_search_index_contract
from phd_searcher.engine.search_documents import (
    SEARCH_INDEX_CONTRACT_PAYLOAD,
    build_candidate_search_document,
)
from phd_searcher.opportunity_kinds import PROGRAMME, UNKNOWN, VACANCY
from phd_searcher.pipeline.family_feedback import (
    FamilyFeedbackProfiles,
    FamilyFeedbackRow,
    FamilyFeedbackSignal,
    build_family_feedback_profiles,
    family_feedback_signal,
)
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.retry import RetryInterruptedError, retry_async
from phd_searcher.pipeline.review_context import (
    application_evidence_supports,
    build_evidence_context,
    classify_opportunity_kind_evidence,
    evidence_quote_present,
    has_future_deadline_status_conflict,
    opportunity_kind_evidence_supports,
    select_evidence_document,
    triage_evidence_supports,
)
from phd_searcher.pipeline.source_family import (
    SOURCE_FAMILY_VERSION,
    FamilyDirection,
    source_family_keys,
)
from phd_searcher.position_types import classify_position
from phd_searcher.screening import detail_rejection_evidence, screen_position

_BATCH = 64
_PAYLOAD_BATCH = 512
_OBSERVATION_PAYLOAD_BATCH = 256
_POSITION_INDEX_KINDS = (VACANCY, PROGRAMME)
_INHERENTLY_VACANCY_POSITION_TYPES = frozenset(
    {
        "internship",
        "assistantship",
        "research_fellowship",
        "postdoc",
        "research_staff",
        "faculty",
    }
)
_PROVISIONAL_INDEX_KINDS = (UNKNOWN, VACANCY, PROGRAMME)
_PROVISIONAL_SCREENING_STATUSES = ("pending", "review", "eligible")
_BLOCKED_PROVISIONAL_REVIEW_STATES = frozenset(
    {"source_broken", "source_unusable", "fetch_unavailable", "fetch_failed"}
)
_BLOCKED_PROVISIONAL_ROUTING_PREFIXES = (
    "evidence:unsupported_",
    "evidence:fragment_unattributable",
    "evidence:host_access_blocked",
    "evidence:access_blocked",
    "evidence:network_unavailable",
    "evidence:fetch_failed",
    "enrich:fragment_evidence_unavailable",
)
_UNSUPPORTED_EVIDENCE_ASSET_SUFFIXES = (
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".webp",
)
VerificationStatus = Literal["verified", "probable"]
UncertaintyFlag = Literal["open_status", "details", "verification", "source_family"]
VerificationMetadata = tuple[
    VerificationStatus,
    float | None,
    int,
    tuple[UncertaintyFlag, ...],
]
FamilySignalPayload = tuple[str | None, int]
# These are deliberately interpretable heuristic tiers, not probabilities.
# They let the user trade precision for recall without changing the audited
# PostgreSQL verdict or waiting for every deep-review call.
_VERIFIED_UNCERTAINTY = 0
_GROUNDED_PROBABLE_UNCERTAINTY = 15
_OPEN_STATUS_UNVERIFIED_UNCERTAINTY = 35
_TITLE_ONLY_CANDIDATE_UNCERTAINTY = 60
_UNGROUNDED_LEGACY_UNCERTAINTY = 85
_TITLE_ONLY_CANDIDATE_TYPES = frozenset(
    {
        "phd",
        "medical_doctorate",
        "internship",
        "assistantship",
        "postdoc",
        "research_staff",
        "faculty",
    }
)
_STATUS_CONFLICT_CANDIDATE_TYPES = _TITLE_ONLY_CANDIDATE_TYPES | {
    "research_fellowship"
}
_EURAXESS_HOST = "euraxess.ec.europa.eu"


def _checkpoint_int(value: object) -> int:
    return int(value) if isinstance(value, (int, str)) else 0


def _normalized_confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    confidence = float(value)
    return confidence if 0 <= confidence <= 1 else None


def _parsed_screening_evidence(position: Position) -> list[str]:
    raw_evidence = getattr(position, "screening_evidence", None)
    if not isinstance(raw_evidence, str):
        return []
    try:
        evidence = json.loads(raw_evidence)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(evidence, list):
        return []
    return [item for item in evidence if isinstance(item, str) and item.strip()]


def _has_authoritative_verified_status(position: Position, *, today: date) -> bool:
    """Return whether ``eligible`` is backed by a final auditable decision.

    Old rule-only positives predate the grounded review cascade. Their stored
    status remains useful input, but calling all of them ``verified`` hides a
    materially different evidence standard. Manual decisions always win;
    automatic verification requires the evidence-grounded reviewer contract.
    """
    if getattr(position, "screening_status", None) != "eligible":
        return False
    if getattr(position, "screening_manual", False):
        return True
    source = str(getattr(position, "screening_source", "") or "")
    version = str(getattr(position, "screening_version", "") or "")
    confidence = _normalized_confidence(
        getattr(position, "screening_confidence", None)
    )
    evidence = _parsed_screening_evidence(position)
    if not (
        source in {"llm", "cache"}
        and version.startswith("evidence-v")
        and getattr(position, "screening_decision", None) == "eligible"
        and getattr(position, "review_state", None) == "resolved"
        and confidence is not None
        and confidence >= 0.9
        and evidence
    ):
        return False
    description = str(getattr(position, "description", "") or "")
    full_description = getattr(position, "full_description", None)
    document = select_evidence_document(
        description,
        full_description if isinstance(full_description, str) else None,
        title=str(getattr(position, "title", "") or ""),
        url=str(getattr(position, "url", "") or ""),
        deadline=getattr(position, "deadline", None),
        deadline_raw=str(getattr(position, "deadline_raw", "") or ""),
        today=today,
    )
    context = build_evidence_context(
        f"{getattr(position, 'title', '') or ''}\n{document}"
    )
    if any(not evidence_quote_present(quote, context) for quote in evidence):
        return False
    position_type = str(getattr(position, "position_type", "other") or "other")
    opportunity_kind = str(
        getattr(position, "opportunity_kind", UNKNOWN) or UNKNOWN
    )
    return application_evidence_supports(
        evidence,
        actual_vacancy="yes",
        open_status="open",
        position_type=position_type,
        today=today,
    ) and opportunity_kind_evidence_supports(
        evidence,
        opportunity_kind,
        today=today,
    )


def _is_current(position: Position, today: date) -> bool:
    deadline = getattr(position, "deadline", None)
    return getattr(position, "is_active", None) is True and (
        deadline is None or deadline >= today
    )


def _has_supported_evidence_route(position: Position) -> bool:
    review_state = str(getattr(position, "review_state", "") or "").casefold()
    if review_state in _BLOCKED_PROVISIONAL_REVIEW_STATES:
        return False
    routing_reason = str(getattr(position, "routing_reason", "") or "").casefold()
    if routing_reason.startswith(_BLOCKED_PROVISIONAL_ROUTING_PREFIXES):
        return False
    try:
        parsed = urlsplit(str(getattr(position, "url", "") or ""))
    except ValueError:
        return False
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return False
    return not parsed.path.casefold().endswith(_UNSUPPORTED_EVIDENCE_ASSET_SUFFIXES)


def _is_euraxess_source(
    position: Position,
    listing_page: ListingPage | None,
) -> bool:
    for value in (
        getattr(position, "url", None),
        getattr(listing_page, "url", None) if listing_page is not None else None,
    ):
        if not isinstance(value, str):
            continue
        try:
            if urlsplit(value).hostname == _EURAXESS_HOST:
                return True
        except ValueError:
            continue
    return False


def _position_evidence(
    position: Position,
    *,
    today: date | None = None,
) -> tuple[str, str, list[str]]:
    """Return the exact grounded text used by the provisional gate."""
    title = str(getattr(position, "title", "") or "")
    description = str(getattr(position, "description", "") or "")
    full_description = getattr(position, "full_description", None)
    evidence = select_evidence_document(
        description,
        full_description if isinstance(full_description, str) else None,
        title=title,
        url=str(getattr(position, "url", "") or ""),
        deadline=getattr(position, "deadline", None),
        deadline_raw=str(getattr(position, "deadline_raw", "") or ""),
        today=today,
    )
    deadline_raw = str(getattr(position, "deadline_raw", "") or "")
    quotes = [quote for quote in (title, evidence, deadline_raw) if quote]
    return title, evidence, quotes


def _supported_opportunity_kind(
    position: Position,
    evidence_quotes: list[str],
    *,
    today: date,
) -> str:
    stored_kind = str(getattr(position, "opportunity_kind", UNKNOWN) or UNKNOWN)
    if stored_kind in _POSITION_INDEX_KINDS and opportunity_kind_evidence_supports(
        evidence_quotes,
        stored_kind,
        today=today,
    ):
        return stored_kind
    return classify_opportunity_kind_evidence(evidence_quotes, today=today)


def _provisional_search_metadata(
    position: Position,
    *,
    today: date,
) -> tuple[str, str]:
    """Derive reversible search labels from the evidence that passed the gate."""
    title, evidence, evidence_quotes = _position_evidence(position, today=today)
    position_type = classify_position(title, evidence)
    stored_type = str(getattr(position, "position_type", "other") or "other")
    if (
        position_type == "other"
        and stored_type != "other"
        and classify_position("", "", explicit=stored_type) != "other"
        and application_evidence_supports(
            evidence_quotes,
            actual_vacancy="yes",
            open_status="open",
            position_type=stored_type,
            today=today,
        )
    ):
        # A source-supplied type may be more specific than the multilingual
        # classifier, but it is retained only when the same evidence supports it.
        position_type = stored_type
    opportunity_kind = _supported_opportunity_kind(
        position,
        evidence_quotes,
        today=today,
    )
    # Shared pages often mention graduate programmes alongside a concrete
    # internship or job.  For inherently role-shaped types, grounded vacancy
    # evidence is more specific than that neighbouring programme language.
    if (
        position_type in _INHERENTLY_VACANCY_POSITION_TYPES
        and opportunity_kind_evidence_supports(
            evidence_quotes,
            VACANCY,
            today=today,
        )
    ):
        opportunity_kind = VACANCY
    return position_type, opportunity_kind


def _search_payload_metadata(
    position: Position,
    verification_status: VerificationStatus,
    *,
    today: date,
) -> tuple[str, str]:
    if verification_status == "verified":
        return position.position_type, position.opportunity_kind
    return _provisional_search_metadata(position, today=today)


def _has_acceptable_provisional_source(
    position: Position,
    listing_page: ListingPage | None,
) -> bool:
    if _is_euraxess_source(position, listing_page):
        return True
    if getattr(position, "listing_page_id", None) is None:
        return True
    # The global quality gate remains a hard prerequisite. A user may choose a
    # less certain candidate, but should not have to reason around a known
    # broken extraction source at the same time.
    return (
        listing_page is not None
        and getattr(listing_page, "quality_status", None) == "healthy"
    )


def _is_audited_curated_portal(listing_page: ListingPage | None) -> bool:
    """Recognize institution-owned sources whose item selector was curated.

    ``seed`` is also used by the global EURAXESS sources; requiring a concrete
    university keeps this exception limited to audited institution portals.
    Their cards establish that an item is an advertised opportunity, but they
    do not fabricate missing detail or current-open evidence.
    """

    return bool(
        listing_page is not None
        and getattr(listing_page, "source", None) == "seed"
        and getattr(listing_page, "university_id", None) is not None
        and getattr(listing_page, "schema_status", None) == "ok"
        and getattr(listing_page, "quality_status", None) == "healthy"
    )


def _provisional_assessment(
    position: Position,
    *,
    listing_page: ListingPage | None = None,
    today: date | None = None,
) -> tuple[int, tuple[UncertaintyFlag, ...]] | None:
    """Return an auditable uncertainty tier for a searchable candidate.

    Hard exclusions remain unchanged. Within those boundaries this is
    intentionally recall-oriented: users can opt into title-grounded leads
    instead of waiting for the GPU cascade to decide every ambiguous record.
    """
    current_day = today or local_today()
    if not _is_current(position, current_day):
        return None
    if getattr(position, "screening_status", None) not in _PROVISIONAL_SCREENING_STATUSES:
        return None
    if getattr(position, "opportunity_kind", None) not in _PROVISIONAL_INDEX_KINDS:
        # In particular, information and spontaneous-application routes stay
        # out of the position index even when semantically related.
        return None
    if not _has_supported_evidence_route(position):
        return None
    if not _has_acceptable_provisional_source(position, listing_page):
        return None
    title, evidence, evidence_quotes = _position_evidence(
        position,
        today=current_day,
    )
    url = str(getattr(position, "url", "") or "")
    status_conflict = has_future_deadline_status_conflict(
        str(getattr(position, "description", "") or ""),
        getattr(position, "full_description", None),
        title=title,
        url=url,
        deadline=getattr(position, "deadline", None),
        deadline_raw=getattr(position, "deadline_raw", None),
        today=current_day,
    )
    if detail_rejection_evidence(evidence) is not None and not status_conflict:
        return None
    rule_decision = screen_position(
        title,
        url,
        evidence,
        str(getattr(position, "position_type", "other") or "other"),
    )
    if rule_decision.status == "rejected" and not status_conflict:
        return None

    # A `/jobs/` URL is useful for routing, but it cannot turn every link on a
    # portal into a probable opportunity (production examples included chefs,
    # nursery managers, course pages and generic grants).  Require the title or
    # candidate-specific text itself to establish both a recognized role and a
    # concrete vacancy/programme shape.  This deliberately favours precision:
    # anything weaker remains auditable in Review and can still be verified by
    # the evidence cascade.
    position_type = str(getattr(position, "position_type", "other") or "other")
    # Do not let shared listing-page text rescue a known navigation/category
    # title.  Oxford's "Administrative Vacancies" and "Operational Vacancies"
    # were concrete production false positives: their page body contains real
    # neighbouring jobs, but the extracted row is not itself an opportunity.
    title_only_decision = screen_position(title, "", "", position_type)
    if title_only_decision.status == "rejected":
        return None
    content_decision = screen_position(
        title,
        "",
        evidence,
        position_type,
    )
    supported_kind = _supported_opportunity_kind(
        position,
        evidence_quotes,
        today=current_day,
    )

    # An audited official portal extracts only individual vacancy cards.  A
    # generic title such as "Interaction Designer" therefore remains a useful
    # lead even when the local reviewer fails to call its tools.  Keep the
    # verdict provisional and expose exactly which facts are still missing.
    # Normal discovered portals do not receive this exception, so shared-page
    # headings and navigation noise remain excluded by the existing guards.
    if _is_audited_curated_portal(listing_page):
        if (
            getattr(position, "full_description", None)
            and triage_evidence_supports(
                evidence_quotes,
                decision="eligible",
                position_type=position_type,
            )
        ):
            return _OPEN_STATUS_UNVERIFIED_UNCERTAINTY, ("open_status",)
        return _TITLE_ONLY_CANDIDATE_UNCERTAINTY, (
            "open_status",
            "details",
        )

    # A future deadline plus a rendered Apply link cannot override an explicit
    # CLOSED/EXPIRED status.  Keep the role searchable only as a clearly
    # labelled lead; deeper evidence or the user decides which signal is stale.
    if status_conflict:
        if (
            classify_position(title, "", explicit=position_type)
            in _STATUS_CONFLICT_CANDIDATE_TYPES
        ):
            return _TITLE_ONLY_CANDIDATE_UNCERTAINTY, (
                "open_status",
                "details",
            )
        return None

    if content_decision.status == "eligible" and supported_kind in _POSITION_INDEX_KINDS:
        if application_evidence_supports(
            evidence_quotes,
            actual_vacancy="yes",
            open_status="open",
            position_type=position_type,
            today=current_day,
        ):
            return _GROUNDED_PROBABLE_UNCERTAINTY, ()
        if triage_evidence_supports(
            evidence_quotes,
            decision="eligible",
            position_type=position_type,
        ):
            return _OPEN_STATUS_UNVERIFIED_UNCERTAINTY, ("open_status",)

    # Strong role-shaped titles are useful leads even when the detail page did
    # not expose an application window. Generic grants/programmes deliberately
    # remain on the evidence route because their titles often denote category
    # pages rather than a single application opportunity.
    title_position_type = classify_position(title, "")
    if title_position_type in _TITLE_ONLY_CANDIDATE_TYPES:
        return _TITLE_ONLY_CANDIDATE_UNCERTAINTY, (
            "open_status",
            "details",
        )
    if (
        getattr(position, "screening_status", None) == "eligible"
        and getattr(position, "opportunity_kind", None) in _POSITION_INDEX_KINDS
    ):
        # Preserve recall for legacy automatic positives while making their
        # weaker provenance explicit. The default GUI cap hides this tier, but
        # users can opt into it without a database migration or another model
        # pass. Hard source, currentness and non-opportunity exclusions above
        # still apply.
        return _UNGROUNDED_LEGACY_UNCERTAINTY, (
            "verification",
            "open_status",
            "details",
        )
    return None


def is_provisional_eligible(
    position: Position,
    *,
    listing_page: ListingPage | None = None,
    today: date | None = None,
) -> bool:
    """Return whether an unresolved record may enter the labelled search index."""
    return (
        _provisional_assessment(
            position,
            listing_page=listing_page,
            today=today,
        )
        is not None
    )


def _provisional_confidence(position: Position) -> float | None:
    # A numeric LLM confidence is meaningful only for a positive raw decision;
    # otherwise the explicit uncertainty tier is the honest signal to expose.
    if getattr(position, "screening_decision", None) != "eligible":
        return None
    return _normalized_confidence(getattr(position, "screening_confidence", None))


def _verification_metadata(
    position: Position,
    today: date,
    *,
    listing_page: ListingPage | None = None,
) -> VerificationMetadata | None:
    if (
        _is_current(position, today)
        and getattr(position, "screening_status", None) == "eligible"
        and getattr(position, "opportunity_kind", None) in _POSITION_INDEX_KINDS
        and _has_authoritative_verified_status(position, today=today)
    ):
        return (
            "verified",
            _normalized_confidence(getattr(position, "screening_confidence", None)),
            _VERIFIED_UNCERTAINTY,
            (),
        )
    assessment = _provisional_assessment(
        position,
        listing_page=listing_page,
        today=today,
    )
    if assessment is not None:
        uncertainty, flags = assessment
        return "probable", _provisional_confidence(position), uncertainty, flags
    return None


async def _load_family_feedback_profiles(
    session: AsyncSession,
) -> FamilyFeedbackProfiles:
    rows = (
        await session.execute(
            select(
                PositionFeedback.position_id,
                PositionFeedback.value,
                PositionFeedback.source_family_keys,
            ).where(
                PositionFeedback.status == "open",
                PositionFeedback.dimension == "opportunity",
                PositionFeedback.source_family_version == SOURCE_FAMILY_VERSION,
                PositionFeedback.value.in_(("yes", "no")),
            )
        )
    ).all()
    return build_family_feedback_profiles(
        FamilyFeedbackRow(
            position_id=position_id,
            value=value,
            family_keys=tuple(
                key for key in (raw_keys or []) if isinstance(key, str)
            ),
        )
        for position_id, value, raw_keys in rows
        if isinstance(value, str) and isinstance(raw_keys, list)
    )


def _family_signal_for_position(
    position: Position,
    listing_page: ListingPage | None,
    profiles: FamilyFeedbackProfiles,
) -> FamilyFeedbackSignal | None:
    return family_feedback_signal(
        position.id,
        source_family_keys(
            position.url,
            listing_url=listing_page.url if listing_page is not None else None,
            listing_page_id=position.listing_page_id,
        ),
        profiles,
    )


def _family_payload(
    verification: VerificationMetadata,
    signal: FamilyFeedbackSignal | None,
) -> tuple[VerificationMetadata, FamilySignalPayload]:
    if signal is None:
        return verification, (None, 0)
    status, confidence, uncertainty, flags = verification
    if (
        status == "probable"
        and signal.direction == FamilyDirection.SUPPORTS_NON_OPPORTUNITY
    ):
        uncertainty = max(uncertainty, _TITLE_ONLY_CANDIDATE_UNCERTAINTY)
        flags = tuple(dict.fromkeys((*flags, "source_family")))
    return (
        (status, confidence, uncertainty, flags),
        (signal.direction.value, signal.samples),
    )


def _unsearchable_indexed_stmt(today: date | None = None) -> Select[tuple[Position]]:
    current_day = today or local_today()
    verified = and_(
        Position.screening_status == "eligible",
        Position.opportunity_kind.in_(_POSITION_INDEX_KINDS),
    )
    provisional = and_(
        Position.screening_status.in_(_PROVISIONAL_SCREENING_STATUSES),
        Position.opportunity_kind.in_(_PROVISIONAL_INDEX_KINDS),
    )
    return select(Position).where(
        Position.indexed_at.is_not(None),
        or_(
            Position.is_active.is_(False),
            and_(Position.deadline.is_not(None), Position.deadline < current_day),
            not_(or_(verified, provisional)),
        ),
    )


def _provisional_indexed_stmt(
    today: date,
) -> Select[tuple[Position, ListingPage]]:
    return select(Position, ListingPage).outerjoin(
        ListingPage,
        Position.listing_page_id == ListingPage.id,
    ).where(
        Position.indexed_at.is_not(None),
        Position.screening_status.in_(_PROVISIONAL_SCREENING_STATUSES),
        Position.opportunity_kind.in_(_PROVISIONAL_INDEX_KINDS),
        Position.is_active.is_(True),
        or_(Position.deadline.is_(None), Position.deadline >= today),
    )


def _positions_to_index_stmt(
    today: date,
) -> Select[tuple[Position, University, ListingPage]]:
    verified = and_(
        Position.screening_status == "eligible",
        Position.opportunity_kind.in_(_POSITION_INDEX_KINDS),
    )
    provisional = and_(
        Position.screening_status.in_(_PROVISIONAL_SCREENING_STATUSES),
        Position.opportunity_kind.in_(_PROVISIONAL_INDEX_KINDS),
    )
    return (
        select(Position, University, ListingPage)
        .outerjoin(University, Position.university_id == University.id)
        .outerjoin(ListingPage, Position.listing_page_id == ListingPage.id)
        .where(Position.indexed_at.is_(None))
        .where(or_(verified, provisional))
        .where(Position.is_active.is_(True))
        .where((Position.deadline.is_(None)) | (Position.deadline >= today))
    )


async def _remove_orphan_vectors(
    qdrant: AsyncQdrantClient,
    collection: str,
    indexed_ids: set[int],
) -> int:
    point_ids: set[int] = set()
    offset = None
    while True:
        points, offset = await qdrant.scroll(
            collection,
            limit=256,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        point_ids.update(point.id for point in points if isinstance(point.id, int))
        if offset is None:
            break
    orphan_ids: list[int | str] = sorted(point_ids - indexed_ids)
    if orphan_ids:
        # The SDK annotates a mutable list with a wider protobuf-aware union.
        # Runtime point IDs here are deliberately restricted to integer IDs.
        await qdrant.delete(
            collection,
            points_selector=cast(Any, orphan_ids),
            wait=True,
        )
    return len(orphan_ids)


async def _ensure_collection(qdrant: AsyncQdrantClient, name: str, dim: int) -> None:
    if not await qdrant.collection_exists(name):
        await qdrant.create_collection(name, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))


async def _invalidate_missing_collection(
    qdrant: AsyncQdrantClient,
    collection: str,
    session: AsyncSession,
) -> int:
    """Make PostgreSQL authoritative when a Qdrant collection disappeared."""
    if await qdrant.collection_exists(collection):
        return 0
    indexed = (
        await session.execute(select(Position).where(Position.indexed_at.is_not(None)))
    ).scalars().all()
    for position in indexed:
        position.indexed_at = None
    if indexed:
        await session.commit()
    return len(indexed)


async def _sync_opportunity_kind_payload(
    qdrant: AsyncQdrantClient,
    collection: str,
    session: AsyncSession,
    *,
    family_profiles: FamilyFeedbackProfiles,
    today: date | None = None,
) -> int:
    """Backfill search/audit metadata on existing vectors without re-embedding."""
    if not await qdrant.collection_exists(collection):
        return 0
    indexed_rows = (
        await session.execute(
            select(Position, ListingPage)
            .outerjoin(ListingPage, Position.listing_page_id == ListingPage.id)
            .where(Position.indexed_at.is_not(None))
        )
    ).all()
    current_day = today or local_today()
    grouped: dict[
        tuple[
            str,
            str,
            VerificationStatus,
            float | None,
            int,
            tuple[UncertaintyFlag, ...],
            str | None,
            int,
        ],
        list[int | str],
    ] = {}
    for position, listing_page in indexed_rows:
        verification = _verification_metadata(
            position,
            current_day,
            listing_page=listing_page,
        )
        if verification is None:
            continue
        verification, family_payload = _family_payload(
            verification,
            _family_signal_for_position(position, listing_page, family_profiles),
        )
        verification_status, confidence, uncertainty_percent, uncertainty_flags = verification
        family_signal, family_samples = family_payload
        position_type, opportunity_kind = _search_payload_metadata(
            position,
            verification_status,
            today=current_day,
        )
        key = (
            position_type,
            opportunity_kind,
            verification_status,
            confidence,
            uncertainty_percent,
            uncertainty_flags,
            family_signal,
            family_samples,
        )
        grouped.setdefault(key, []).append(position.id)
    for (
        position_type,
        opportunity_kind,
        verification_status,
        confidence,
        uncertainty_percent,
        uncertainty_flags,
        family_signal,
        family_samples,
    ), position_ids in grouped.items():
        for start in range(0, len(position_ids), _PAYLOAD_BATCH):
            await qdrant.set_payload(
                collection_name=collection,
                payload={
                    "position_type": position_type,
                    "opportunity_kind": opportunity_kind,
                    "verification_status": verification_status,
                    "confidence": confidence,
                    "uncertainty_percent": uncertainty_percent,
                    "uncertainty_flags": list(uncertainty_flags),
                    "source_family_signal": family_signal,
                    "source_family_samples": family_samples,
                    "source_family_version": SOURCE_FAMILY_VERSION,
                },
                points=cast(Any, position_ids[start : start + _PAYLOAD_BATCH]),
                wait=True,
            )
    return len(indexed_rows)


def _utc_payload_timestamp(value: datetime | None) -> str | None:
    return value.replace(tzinfo=UTC).isoformat() if value is not None else None


async def _sync_observation_payload(
    qdrant: AsyncQdrantClient,
    collection: str,
    session: AsyncSession,
) -> int:
    """Refresh acquisition timestamps without recalculating any embedding."""
    if not await qdrant.collection_exists(collection):
        return 0
    rows = (
        await session.execute(
            select(Position.id, Position.first_seen_at, Position.scraped_at).where(
                Position.indexed_at.is_not(None)
            )
        )
    ).all()
    for start in range(0, len(rows), _OBSERVATION_PAYLOAD_BATCH):
        operations = [
            SetPayloadOperation(
                set_payload=SetPayload(
                    payload={
                        "first_seen_at": _utc_payload_timestamp(first_seen_at),
                        "last_seen_at": _utc_payload_timestamp(last_seen_at),
                        # Retained for API compatibility with existing vectors.
                        "scraped_at": _utc_payload_timestamp(last_seen_at),
                    },
                    points=[position_id],
                )
            )
            for position_id, first_seen_at, last_seen_at in rows[
                start : start + _OBSERVATION_PAYLOAD_BATCH
            ]
        ]
        if operations:
            await qdrant.batch_update_points(
                collection_name=collection,
                update_operations=operations,
                wait=True,
            )
    return len(rows)


async def _embed_and_upsert(
    model: ModelHelper,
    qdrant: AsyncQdrantClient,
    collection: str,
    batch: list[tuple[Position, University | None, ListingPage | None]],
    *,
    family_profiles: FamilyFeedbackProfiles,
    today: date,
) -> list[PointStruct]:
    prepared: list[
        tuple[
            Position,
            University | None,
            VerificationMetadata,
            FamilySignalPayload,
            str,
            str,
        ]
    ] = []
    for position, university, listing_page in batch:
        verification = _verification_metadata(
            position,
            today,
            listing_page=listing_page,
        )
        if verification is None:
            raise ValueError(
                f"position {position.id} no longer satisfies index eligibility"
            )
        verification, family_payload = _family_payload(
            verification,
            _family_signal_for_position(position, listing_page, family_profiles),
        )
        position_type, opportunity_kind = _search_payload_metadata(
            position,
            verification[0],
            today=today,
        )
        prepared.append(
            (
                position,
                university,
                verification,
                family_payload,
                position_type,
                opportunity_kind,
            )
        )

    vectors = await model.embed_documents(
        [
            build_candidate_search_document(
                title=position.title,
                position_type=position_type,
                institution=(
                    university.name
                    if university is not None
                    else (position.institution_name or "")
                ),
                description=position.description,
            )
            for position, university, _, _, position_type, _ in prepared
        ]
    )
    await _ensure_collection(qdrant, collection, dim=len(vectors[0]))
    points: list[PointStruct] = []
    for (
        (
            position,
            university,
            verification,
            family_payload,
            position_type,
            opportunity_kind,
        ),
        vector,
    ) in zip(
        prepared,
        vectors,
        strict=True,
    ):
        verification_status, confidence, uncertainty_percent, uncertainty_flags = verification
        family_signal, family_samples = family_payload
        points.append(
            PointStruct(
                id=position.id,
                vector=vector,
                payload={
                    "title": position.title,
                    "url": position.url,
                    "university": university.name if university else (position.institution_name or ""),
                    "country": university.country if university else (position.institution_country or ""),
                    "deadline": position.deadline.isoformat() if position.deadline else None,
                    "deadline_ts": (
                        datetime.combine(position.deadline, time.min, tzinfo=UTC).isoformat()
                        if position.deadline
                        else None
                    ),
                    "first_seen_at": _utc_payload_timestamp(
                        getattr(position, "first_seen_at", None)
                    ),
                    "last_seen_at": _utc_payload_timestamp(position.scraped_at),
                    "scraped_at": _utc_payload_timestamp(position.scraped_at),
                    "duration": position.duration_raw,
                    "compensation": position.compensation_raw,
                    "compensation_min": position.compensation_min,
                    "compensation_max": position.compensation_max,
                    "compensation_currency": position.compensation_currency,
                    "compensation_period": position.compensation_period,
                    "position_type": position_type,
                    "opportunity_kind": opportunity_kind,
                    "verification_status": verification_status,
                    "confidence": confidence,
                    "uncertainty_percent": uncertainty_percent,
                    "uncertainty_flags": list(uncertainty_flags),
                    "source_family_signal": family_signal,
                    "source_family_samples": family_samples,
                    "source_family_version": SOURCE_FAMILY_VERSION,
                    SEARCH_INDEX_CONTRACT_PAYLOAD: model.search_index_contract(),
                    "published": position.published_at.isoformat() if position.published_at else None,
                    "published_ts": (
                        datetime.combine(position.published_at, time.min, tzinfo=UTC).isoformat()
                        if position.published_at
                        else None
                    ),
                },
            )
        )
    await qdrant.upsert(collection, points=points, wait=True)
    return points


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    """Ritorna il numero di posizioni indicizzate, opzionalmente per istituzione."""
    progress = progress or Progress()
    qdrant = container.get(AsyncQdrantClient)
    config = container.get(QdrantConfig)
    model = container.get(ModelHelper)
    session_maker = container.get(async_sessionmaker[AsyncSession])
    today = local_today()
    checkpoint = await progress.load_checkpoint()
    processed = _checkpoint_int(checkpoint.get("processed", 0))
    remaining = None if limit is None else max(limit - processed, 0)
    full_refresh = limit is None and name_like is None

    async with session_maker() as session:
        await validate_search_index_contract(
            qdrant,
            config.collection,
            model.search_index_contract(),
        )
        family_profiles = await _load_family_feedback_profiles(session)
        invalidated = await _invalidate_missing_collection(
            qdrant,
            config.collection,
            session,
        )
        if invalidated:
            print(
                "index: collection missing; marked "
                f"{invalidated} positions for reconstruction"
            )

        # Deadline extraction belongs to scrape/enrich. Historical parser-wide
        # repairs must be explicit, versioned migrations: scanning hundreds of
        # megabytes of unchanged detail text here blocked the single API event
        # loop on every otherwise cheap index reconciliation.

        # PostgreSQL resta autorevole e conserva ogni record. Qdrant include i
        # verified e solo i provisional che superano ancora il gate economico.
        unsearchable = {
            position.id: position
            for position in (
                await session.execute(_unsearchable_indexed_stmt(today))
            ).scalars().all()
        }
        provisional_indexed = (
            await session.execute(_provisional_indexed_stmt(today))
        ).all()
        for position, listing_page in provisional_indexed:
            if _verification_metadata(
                position,
                listing_page=listing_page,
                today=today,
            ) is None:
                unsearchable[position.id] = position
        if unsearchable:
            for position in unsearchable.values():
                position.indexed_at = None
            await session.commit()
            print(f"index: marked {len(unsearchable)} non-searchable positions stale")

        # Elimina vettori stale prima di applicare `limit`: Qdrant e il flag DB
        # restano coerenti anche durante una run di prova parziale.
        stale_ids = (
            await session.execute(select(Position.id).where(Position.indexed_at.is_(None)))
        ).scalars().all()
        if stale_ids and await qdrant.collection_exists(config.collection):
            await qdrant.delete(config.collection, points_selector=list(stale_ids), wait=True)
            print(f"index: removed {len(stale_ids)} stale vectors")

        if full_refresh:
            payload_synced = await _sync_opportunity_kind_payload(
                qdrant,
                config.collection,
                session,
                family_profiles=family_profiles,
                today=today,
            )
            if payload_synced:
                print(
                    "index: synchronized search metadata for "
                    f"{payload_synced} positions"
                )
            observation_synced = await _sync_observation_payload(
                qdrant,
                config.collection,
                session,
            )
            if observation_synced:
                print(
                    "index: synchronized acquisition timestamps for "
                    f"{observation_synced} positions"
                )

        # Indicizza verified e provisional attivi non ancora presenti.
        stmt = _positions_to_index_stmt(today)
        if name_like:
            stmt = stmt.where(University.name.ilike(f"%{name_like}%"))
        candidate_rows = (await session.execute(stmt)).all()
        rows = [
            row
            for row in candidate_rows
            if _verification_metadata(
                row[0],
                today,
                listing_page=row[2],
            )
            is not None
        ]
        if remaining is not None:
            rows = rows[:remaining]

        total = processed
        await progress.begin((len(rows) + _BATCH - 1) // _BATCH)
        for start in range(0, len(rows), _BATCH):
            if progress.should_stop:
                break
            await progress.tick(f"batch {start // _BATCH + 1}")
            batch = [
                (row[0], row[1], row[2])
                for row in rows[start : start + _BATCH]
            ]
            batch_key = f"index:{batch[0][0].id}:{batch[-1][0].id}"

            async def embed_current(
                batch_to_embed: list[
                    tuple[Position, University | None, ListingPage | None]
                ] = batch,
            ) -> list[PointStruct]:
                return await _embed_and_upsert(
                    model,
                    qdrant,
                    config.collection,
                    batch_to_embed,
                    family_profiles=family_profiles,
                    today=today,
                )

            try:
                points = await retry_async(
                    progress,
                    batch_key,
                    embed_current,
                )
            except RetryInterruptedError:
                break
            now = datetime.now(UTC).replace(tzinfo=None)  # colonna naive: asyncpg rifiuta datetime tz-aware
            for p, _, _ in batch:
                p.indexed_at = now
            await session.commit()
            total += len(points)
            await progress.save_checkpoint(processed=total, last_position_id=batch[-1][0].id)

        # Una run completa riconcilia anche punti Qdrant senza più una riga DB
        # indicizzata (per esempio record cancellati o vettori legacy). Le run
        # limitate/per-name non possono stabilire l'insieme globale e non puliscono.
        if full_refresh and not progress.should_stop and await qdrant.collection_exists(config.collection):
            indexed_ids = set(
                (
                    await session.execute(
                        select(Position.id).where(Position.indexed_at.is_not(None))
                    )
                ).scalars().all()
            )
            removed = await _remove_orphan_vectors(qdrant, config.collection, indexed_ids)
            if removed:
                print(f"index: removed {removed} orphan vectors")
        print(f"index: upserted {total} positions")
    return total
