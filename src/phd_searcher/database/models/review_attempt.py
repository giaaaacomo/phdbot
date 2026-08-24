"""Verdetto immutabile prodotto da ogni livello della cascata di review."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phd_searcher.database.models.base import Base


class ReviewAttempt(Base):
    """Audit append-only: non sovrascrive mai il verdetto corrente di Position."""

    __tablename__ = "review_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(
        ForeignKey("positions.id", ondelete="CASCADE"),
        index=True,
    )
    pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
        index=True,
    )
    stage: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(64))
    raw_decision: Mapped[str] = mapped_column(String(16))
    accepted_status: Mapped[str] = mapped_column(String(16), index=True)
    position_type: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[list[str]] = mapped_column(JSONB, default=list)
    reason: Mapped[str | None] = mapped_column(Text)
    tool_attempts: Mapped[int] = mapped_column(Integer, default=1)
    latency_seconds: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
