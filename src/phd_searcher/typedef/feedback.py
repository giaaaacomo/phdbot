"""Structured request and response types for position feedback."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

FeedbackReason = Literal[
    "confirmed_opportunity",
    "non_opportunity",
    "closed",
    "duplicate",
    "wrong_type",
    "mismatched_details",
    "broken_link",
    "other",
]
FeedbackStatus = Literal["open", "retracted"]
FeedbackDimension = Literal[
    "opportunity",
    "availability",
    "type",
    "extraction",
    "reachability",
    "duplicate",
    "other",
]


class PositionFeedbackCreate(BaseModel):
    reason: FeedbackReason
    note: str | None = Field(default=None, max_length=2_000)

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class PositionFeedbackView(BaseModel):
    id: int
    position_id: int
    reason: FeedbackReason
    dimension: FeedbackDimension
    value: str
    note: str | None = None
    source_family_version: str | None = None
    source_family_keys: list[str] = Field(default_factory=list)
    status: FeedbackStatus
    created_at: datetime
    retracted_at: datetime | None = None
