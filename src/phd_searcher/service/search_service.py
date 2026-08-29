"""Ricerca semantica: embed della query (litellm) + query Qdrant con filtri."""

from __future__ import annotations

import asyncio
import re
import time as monotonic_time
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, time
from typing import cast

import httpx
from injector import inject
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Condition,
    DatetimeRange,
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchAny,
    MatchValue,
    PayloadField,
    Range,
    ScoredPoint,
)

from phd_searcher.config.qdrant import QdrantConfig
from phd_searcher.engine.model_helper import ModelHelper
from phd_searcher.engine.search_query import split_combined_query
from phd_searcher.opportunity_kinds import normalize_opportunity_kind
from phd_searcher.position_types import classify_position
from phd_searcher.typedef.search import (
    InstitutionHit,
    SearchBody,
    SearchHit,
    SearchResult,
    UncertaintyFlag,
    VerificationStatus,
)

_ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_RATES_TTL_SECONDS = 12 * 60 * 60
_rates: dict[str, float] = {"EUR": 1.0}
_rates_loaded_at = 0.0
_RETRIEVAL_ACRONYMS = {
    "xr": "extended reality",
    "vr": "virtual reality",
    "ar": "augmented reality",
    "hci": "human-computer interaction",
    "ixd": "interaction design",
}
_RETRIEVAL_PHRASES = {
    "design dell'interazione": "interaction design",
    "design dell\u2019interazione": "interaction design",
    "progettazione dell'interazione": "interaction design",
    "progettazione dell\u2019interazione": "interaction design",
    "ingegneria navale": "naval engineering",
    "calcolo spaziale": "spatial computing",
    "realtà estesa": "extended reality",
    "realta estesa": "extended reality",
    "realtà virtuale": "virtual reality",
    "realta virtuale": "virtual reality",
}
_RETRIEVAL_PHRASE = re.compile(
    "|".join(re.escape(phrase) for phrase in sorted(_RETRIEVAL_PHRASES, key=len, reverse=True)),
    re.I,
)
_RETRIEVAL_ACRONYM = re.compile(
    r"\b(?:" + "|".join(_RETRIEVAL_ACRONYMS) + r")\b",
    re.I,
)


def normalize_retrieval_query(value: str) -> str:
    """Canonicalize audited domain phrases and unambiguous acronyms.

    Replacing the token preserves the rest of a user's query and avoids the
    dilution observed when a long synonym list is appended to short queries.
    """
    value = _RETRIEVAL_PHRASE.sub(
        lambda match: _RETRIEVAL_PHRASES[match.group(0).casefold()],
        value,
    )
    return _RETRIEVAL_ACRONYM.sub(
        lambda match: _RETRIEVAL_ACRONYMS[match.group(0).casefold()],
        value,
    )


def normalized_retrieval_queries(value: str) -> list[str]:
    """Return de-duplicated OR clauses after audited query normalization."""
    queries: list[str] = []
    seen: set[str] = set()
    for part in split_combined_query(value):
        normalized = normalize_retrieval_query(part).strip()
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            queries.append(normalized)
    return queries


def _fuse_points(point_groups: Iterable[Sequence[ScoredPoint]]) -> list[ScoredPoint]:
    """OR-fuse semantic searches, keeping each point's best similarity."""
    best: dict[int, ScoredPoint] = {}
    for points in point_groups:
        for point in points:
            point_id = int(point.id)
            previous = best.get(point_id)
            if previous is None or point.score > previous.score:
                best[point_id] = point
    return sorted(best.values(), key=lambda point: (-point.score, int(point.id)))


def _range_or_unknown(condition: FieldCondition, *, key: str) -> Filter:
    """Apply a range to known values while retaining missing values."""
    return Filter(
        should=[
            condition,
            IsEmptyCondition(is_empty=PayloadField(key=key)),
        ]
    )


def _has_unknown_compensation(hit: SearchHit) -> bool:
    return hit.compensation_eur_max is None


def _payload_confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    confidence = float(value)
    return confidence if 0 <= confidence <= 1 else None


def _payload_uncertainty(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return default
    return round(max(0.0, min(float(value), 100.0)))


def _payload_uncertainty_flags(value: object) -> list[UncertaintyFlag]:
    if not isinstance(value, list):
        return []
    allowed: set[str] = {"open_status", "details", "verification", "source_family"}
    return cast(
        list[UncertaintyFlag],
        list(dict.fromkeys(item for item in value if isinstance(item, str) and item in allowed)),
    )


async def _euro_rates() -> dict[str, float]:
    """Tassi ECB (unità di valuta per EUR), con cache e fallback non bloccante."""
    global _rates, _rates_loaded_at
    now = monotonic_time.monotonic()
    if _rates_loaded_at > 0 and now - _rates_loaded_at < _RATES_TTL_SECONDS:
        return _rates
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(_ECB_RATES_URL)
            response.raise_for_status()
        parsed = {"EUR": 1.0}
        for element in ET.fromstring(response.content).iter():
            currency, rate = element.attrib.get("currency"), element.attrib.get("rate")
            if currency and rate:
                parsed[currency] = float(rate)
        _rates = parsed
    except (httpx.HTTPError, ET.ParseError, ValueError):
        pass
    _rates_loaded_at = now
    return _rates


class SearchService:
    @inject
    def __init__(self, model: ModelHelper, qdrant: AsyncQdrantClient, config: QdrantConfig) -> None:
        self._model = model
        self._qdrant = qdrant
        self._collection = config.collection

    async def search(self, body: SearchBody) -> SearchResult:
        queries = normalized_retrieval_queries(body.query)
        vectors = await self._model.embed_queries(queries)
        must: list[Condition] = []
        must_not: list[Condition] = []
        if body.mode == "verified_only":
            # Verification is an explicit evidence claim. Missing legacy
            # metadata must never be silently promoted to verified.
            must.append(
                FieldCondition(
                    key="verification_status",
                    match=MatchValue(value="verified"),
                )
            )
        if body.max_uncertainty is not None:
            must.append(
                FieldCondition(
                    key="uncertainty_percent",
                    range=Range(lte=float(body.max_uncertainty)),
                )
            )
        countries = list(dict.fromkeys(([body.country] if body.country else []) + body.countries))
        universities = list(dict.fromkeys(([body.university] if body.university else []) + body.universities))
        if countries:
            must.append(FieldCondition(key="country", match=MatchAny(any=countries)))
        if universities:
            must.append(FieldCondition(key="university", match=MatchAny(any=universities)))
        if body.deadline_after or body.deadline_before:
            must.append(
                _range_or_unknown(
                    FieldCondition(
                        key="deadline_ts",
                        range=DatetimeRange(
                            gte=datetime.combine(body.deadline_after, time.min, tzinfo=UTC)
                            if body.deadline_after
                            else None,
                            lte=datetime.combine(body.deadline_before, time.max, tzinfo=UTC)
                            if body.deadline_before
                            else None,
                        ),
                    ),
                    key="deadline_ts",
                )
            )
        if body.posted_after or body.posted_before:
            must.append(
                _range_or_unknown(
                    FieldCondition(
                        key="published_ts",
                        range=DatetimeRange(
                            gte=datetime.combine(body.posted_after, time.min, tzinfo=UTC)
                            if body.posted_after
                            else None,
                            lte=datetime.combine(body.posted_before, time.max, tzinfo=UTC)
                            if body.posted_before
                            else None,
                        ),
                    ),
                    key="published_ts",
                )
            )
        count = (await self._qdrant.count(self._collection, exact=True)).count
        if count == 0:
            return SearchResult(hits=[], total=0)
        institutions: list[InstitutionHit] = []
        institution_collection = f"{self._collection}_institutions"
        if await self._qdrant.collection_exists(institution_collection):
            institution_must: list[FieldCondition] = []
            if countries:
                institution_must.append(FieldCondition(key="country", match=MatchAny(any=countries)))
            if universities:
                institution_must.append(FieldCondition(key="university", match=MatchAny(any=universities)))
            institution_responses = await asyncio.gather(
                *(
                    self._qdrant.query_points(
                        institution_collection,
                        query=vector,
                        limit=12,
                        score_threshold=max(body.min_score - 0.2, 0.25),
                        query_filter=(Filter(must=institution_must) if institution_must else None),
                    )
                    for vector in vectors
                )
            )
            institution_points = _fuse_points(response.points for response in institution_responses)[:12]
            institutions = [
                InstitutionHit(
                    score=point.score,
                    name=str((point.payload or {}).get("name", "")),
                    kind=str((point.payload or {}).get("kind", "university")),
                    university=str((point.payload or {}).get("university", "")),
                    country=str((point.payload or {}).get("country", "")),
                    url=str((point.payload or {}).get("url", "")),
                    spontaneous_application_url=(point.payload or {}).get("spontaneous_application_url"),
                    active_positions=int((point.payload or {}).get("active_positions", 0)),
                )
                for point in institution_points
            ]
        responses = await asyncio.gather(
            *(
                self._qdrant.query_points(
                    self._collection,
                    query=vector,
                    limit=count,
                    score_threshold=body.min_score,
                    query_filter=(Filter(must=list(must), must_not=list(must_not)) if must or must_not else None),
                )
                for vector in vectors
            )
        )
        points = _fuse_points(response.points for response in responses)
        hits: list[SearchHit] = []
        for point in points:
            payload = point.payload or {}
            raw_verification_status = payload.get("verification_status")
            verification_status = "verified" if raw_verification_status == "verified" else "probable"
            verification_metadata_missing = raw_verification_status not in {
                "verified",
                "probable",
            }
            uncertainty_flags = _payload_uncertainty_flags(payload.get("uncertainty_flags"))
            if verification_metadata_missing and "verification" not in uncertainty_flags:
                uncertainty_flags.append("verification")
            hits.append(
                SearchHit(
                    position_id=int(point.id),
                    score=point.score,
                    title=str(payload.get("title", "")),
                    university=str(payload.get("university", "")),
                    country=str(payload.get("country", "")),
                    url=str(payload.get("url", "")),
                    deadline=payload.get("deadline"),
                    published_at=payload.get("published"),
                    first_seen_at=payload.get("first_seen_at"),
                    last_seen_at=payload.get("last_seen_at") or payload.get("scraped_at"),
                    scraped_at=payload.get("scraped_at"),
                    duration=payload.get("duration"),
                    compensation=payload.get("compensation"),
                    compensation_min=payload.get("compensation_min"),
                    compensation_max=payload.get("compensation_max"),
                    compensation_currency=payload.get("compensation_currency"),
                    compensation_period=payload.get("compensation_period"),
                    position_type=classify_position(
                        str(payload.get("title", "")),
                        explicit=payload.get("position_type"),
                    ),
                    opportunity_kind=normalize_opportunity_kind(payload.get("opportunity_kind")),
                    verification_status=cast(
                        VerificationStatus,
                        verification_status,
                    ),
                    confidence=_payload_confidence(payload.get("confidence")),
                    uncertainty_percent=(
                        100
                        if verification_metadata_missing
                        else _payload_uncertainty(
                            payload.get("uncertainty_percent"),
                            default=100 if verification_status == "probable" else 0,
                        )
                    ),
                    uncertainty_flags=uncertainty_flags,
                    source_family_signal=payload.get("source_family_signal"),
                    source_family_samples=int(payload.get("source_family_samples") or 0),
                    source_family_version=payload.get("source_family_version"),
                )
            )
        if body.position_types:
            hits = [hit for hit in hits if hit.position_type in body.position_types]

        currencies = {hit.compensation_currency for hit in hits if hit.compensation_currency not in (None, "EUR")}
        rates = await _euro_rates() if currencies else {"EUR": 1.0}
        for hit in hits:
            rate = rates.get(hit.compensation_currency or "")
            if rate:
                hit.compensation_eur_min = hit.compensation_min / rate if hit.compensation_min is not None else None
                hit.compensation_eur_max = hit.compensation_max / rate if hit.compensation_max is not None else None
        if body.compensation_min is not None:
            hits = [
                hit
                for hit in hits
                if hit.compensation_eur_max is None or hit.compensation_eur_max >= body.compensation_min
            ]
        if body.sort_by == "relevance":
            hits.sort(
                key=lambda hit: hit.score,
                reverse=body.sort_order == "desc",
            )
        else:
            key_name = {
                "uncertainty": "uncertainty_percent",
                "compensation": "compensation_eur_max",
                "posted": "published_at",
                "deadline": "deadline",
                "country": "country",
            }[body.sort_by]
            available = [hit for hit in hits if getattr(hit, key_name) not in (None, "")]
            missing = [hit for hit in hits if getattr(hit, key_name) in (None, "")]
            available.sort(key=lambda hit: getattr(hit, key_name), reverse=body.sort_order == "desc")
            hits = available + missing
        if body.compensation_min is not None:
            # Stable partition: matching known values first, retained unknowns
            # second, without changing the requested ordering within either.
            hits = [hit for hit in hits if not _has_unknown_compensation(hit)] + [
                hit for hit in hits if _has_unknown_compensation(hit)
            ]
        total = len(hits)
        if body.limit is not None:
            hits = hits[: body.limit]
        return SearchResult(hits=hits, total=total, institutions=institutions)
