"""Request/response types per search e coverage. Pure data — no behaviour."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from phd_searcher.countries import country_code
from phd_searcher.opportunity_kinds import DEFAULT_OPPORTUNITY_KIND, OpportunityKind
from phd_searcher.position_types import POSITION_TYPES

ScreeningStatus = Literal["pending", "eligible", "review", "rejected", "quarantine"]
ManualScreeningStatus = Literal["eligible", "review", "rejected"]
SearchMode = Literal["verified_only", "include_probable"]
VerificationStatus = Literal["verified", "probable"]
UncertaintyFlag = Literal["open_status", "details", "verification", "source_family"]
SourceFamilySignal = Literal["supports_opportunity", "supports_non_opportunity"]


class SearchBody(BaseModel):
    query: str
    mode: SearchMode = "verified_only"
    # Heuristic audit score, not a calibrated probability. ``None`` keeps all
    # results allowed by ``mode``; 0 is equivalent to fully verified only.
    max_uncertainty: int | None = Field(default=None, ge=0, le=100)
    country: str | None = None  # ISO alpha-2, es. "IT"
    countries: list[str] = Field(default_factory=list)
    university: str | None = None  # nome esatto dell'ateneo (match sul payload Qdrant)
    universities: list[str] = Field(default_factory=list)
    position_types: list[str] = Field(default_factory=list)
    deadline_after: date | None = None
    deadline_before: date | None = None
    posted_after: date | None = None
    posted_before: date | None = None
    compensation_min: float | None = Field(default=None, ge=0)
    min_score: float = Field(default=0.6, ge=-1, le=1)
    sort_by: Literal[
        "relevance",
        "uncertainty",
        "compensation",
        "posted",
        "deadline",
        "country",
    ] = "relevance"
    sort_order: Literal["asc", "desc"] = "desc"
    limit: int | None = Field(default=None, ge=1)

    @field_validator("country", mode="before")
    @classmethod
    def normalize_country(cls, value: object) -> str | None:
        if value is None or not str(value).strip():
            return None
        normalized = country_code(value)
        if normalized is None:
            raise ValueError("unknown country; use ISO alpha-2/alpha-3 or an English/Italian country name")
        return normalized

    @field_validator("countries", mode="before")
    @classmethod
    def normalize_countries(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized = [country_code(item) for item in value]
        if any(item is None for item in normalized):
            raise ValueError("countries contain an unknown value")
        return list(dict.fromkeys(item for item in normalized if item is not None))

    @field_validator("position_types")
    @classmethod
    def validate_position_types(cls, value: list[str]) -> list[str]:
        unknown = set(value) - POSITION_TYPES.keys()
        if unknown:
            raise ValueError(f"unknown position types: {', '.join(sorted(unknown))}")
        return list(dict.fromkeys(value))


class SearchHit(BaseModel):
    position_id: int
    score: float
    title: str
    university: str
    country: str
    url: str
    deadline: date | None = None
    published_at: date | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    # Backwards-compatible alias used by older clients.
    scraped_at: datetime | None = None
    duration: str | None = None
    compensation: str | None = None
    compensation_min: float | None = None
    compensation_max: float | None = None
    compensation_currency: str | None = None
    compensation_period: str | None = None
    compensation_eur_min: float | None = None
    compensation_eur_max: float | None = None
    position_type: str = "other"
    opportunity_kind: OpportunityKind = DEFAULT_OPPORTUNITY_KIND
    verification_status: VerificationStatus = "verified"
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainty_percent: int = Field(default=0, ge=0, le=100)
    uncertainty_flags: list[UncertaintyFlag] = Field(default_factory=list)
    source_family_signal: SourceFamilySignal | None = None
    source_family_samples: int = Field(default=0, ge=0)
    source_family_version: str | None = None


class InstitutionHit(BaseModel):
    score: float
    name: str
    kind: str
    university: str
    country: str
    url: str
    spontaneous_application_url: str | None = None
    active_positions: int = 0


class SearchResult(BaseModel):
    hits: list[SearchHit]
    total: int
    institutions: list[InstitutionHit] = Field(default_factory=list)


class PositionDetail(BaseModel):
    id: int
    title: str
    url: str
    description: str
    deadline: date | None
    deadline_raw: str | None
    published_raw: str | None
    published_at: date | None
    first_seen_at: datetime | None
    last_seen_at: datetime
    # Backwards-compatible alias used by older clients.
    scraped_at: datetime
    duration: str | None
    compensation: str | None
    compensation_min: float | None
    compensation_max: float | None
    compensation_currency: str | None
    compensation_period: str | None
    position_type: str
    opportunity_kind: OpportunityKind = DEFAULT_OPPORTUNITY_KIND
    university: str
    country: str


class PositionLookup(BaseModel):
    found: bool
    position: PositionDetail | None = None


class ScreeningItem(BaseModel):
    id: int
    title: str
    url: str
    description: str
    position_type: str
    opportunity_kind: OpportunityKind = DEFAULT_OPPORTUNITY_KIND
    status: ScreeningStatus
    reason: str | None = None
    manual: bool = False
    source: str = "rules"
    decision: ScreeningStatus | None = None
    confidence: float | None = None
    evidence: list[str] = Field(default_factory=list)
    model: str | None = None
    version: str | None = None
    screened_at: datetime | None = None
    review_state: str = "untriaged"
    routing_reason: str | None = None
    university: str
    country: str


class ScreeningPage(BaseModel):
    items: list[ScreeningItem]
    total: int
    counts: dict[str, int]
    limit: int
    offset: int


class ReviewAttemptItem(BaseModel):
    id: int
    stage: str
    model: str | None = None
    version: str
    raw_decision: str
    accepted_status: str
    position_type: str | None = None
    confidence: float | None = None
    evidence: list[str] = Field(default_factory=list)
    reason: str | None = None
    tool_attempts: int = 1
    latency_seconds: float | None = None
    details: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class ScreeningUpdate(BaseModel):
    status: ManualScreeningStatus
    reason: str | None = Field(default=None, max_length=220)


ExportFormat = Literal["html", "pdf", "csv", "json"]


class ExportBody(BaseModel):
    search: SearchBody
    format: ExportFormat = "html"
    title: str | None = Field(default=None, max_length=160)


class UniversityCoverage(BaseModel):
    name: str
    country: str
    website_url: str
    discovery_status: str
    catalog_tier: Literal["core", "specialist"] = "core"
    catalog_basis: str = "wikidata:Q3918"
    listing_pages_count: int
    listing_pages_ok: int
    listing_pages_quarantined: int = 0
    positions_count: int
    positions_quarantined: int = 0


class CoverageResult(BaseModel):
    universities: list[UniversityCoverage]


class SearchFacetInstitution(BaseModel):
    name: str
    country: str


class SearchFacets(BaseModel):
    countries: list[str]
    institutions: list[SearchFacetInstitution]
    position_types: dict[str, str]
