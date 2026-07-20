"""Request/response types per search e coverage. Pure data — no behaviour."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class SearchBody(BaseModel):
    query: str
    country: str | None = None  # ISO alpha-2, es. "IT"
    university: str | None = None  # nome esatto dell'ateneo (match sul payload Qdrant)
    deadline_after: date | None = None
    limit: int = Field(default=10, ge=1, le=100)


class SearchHit(BaseModel):
    position_id: int
    score: float
    title: str
    university: str
    country: str
    url: str
    deadline: date | None = None


class SearchResult(BaseModel):
    hits: list[SearchHit]


class PositionDetail(BaseModel):
    id: int
    title: str
    url: str
    description: str
    deadline: date | None
    deadline_raw: str | None
    university: str
    country: str


class PositionLookup(BaseModel):
    found: bool
    position: PositionDetail | None = None


class UniversityCoverage(BaseModel):
    name: str
    country: str
    website_url: str
    discovery_status: str
    listing_pages_count: int
    listing_pages_ok: int
    positions_count: int


class CoverageResult(BaseModel):
    universities: list[UniversityCoverage]
