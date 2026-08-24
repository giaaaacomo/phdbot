"""Persistent one-shot schedules for pipeline and macro workflows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phd_searcher.database.models.base import Base


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    target: Mapped[str] = mapped_column(String(16), index=True)
    state: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    # Il progetto usa timestamp UTC-naive in Postgres; l'API restituisce sempre
    # un offset esplicito e conserva separatamente il fuso scelto dall'utente.
    run_at: Mapped[datetime] = mapped_column(index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Rome")
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    macro_id: Mapped[int | None] = mapped_column(ForeignKey("saved_macros.id", ondelete="CASCADE"), index=True)
    pipeline_run_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_runs.id", ondelete="SET NULL"))
    macro_run_id: Mapped[int | None] = mapped_column(ForeignKey("macro_runs.id", ondelete="SET NULL"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(index=True)
    lease_until: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
