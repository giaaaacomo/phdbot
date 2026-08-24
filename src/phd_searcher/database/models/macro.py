"""Saved refresh/search/export workflows and their durable executions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phd_searcher.database.models.base import Base


class SavedMacro(Base):
    __tablename__ = "saved_macros"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    refresh: Mapped[bool] = mapped_column(Boolean, default=True)
    pipeline_params: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    search_body: Mapped[dict[str, object]] = mapped_column(JSONB)
    export_formats: Mapped[list[str]] = mapped_column(JSONB, default=lambda: ["html"])
    destination: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class MacroRun(Base):
    __tablename__ = "macro_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    scheduled_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_jobs.id", ondelete="SET NULL"), unique=True, index=True
    )
    macro_id: Mapped[int] = mapped_column(ForeignKey("saved_macros.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    current_step: Mapped[str | None] = mapped_column(String(64))
    pipeline_run_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_runs.id", ondelete="SET NULL"))
    outputs: Mapped[list[str]] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
