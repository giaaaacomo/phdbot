"""Una riga per run della pipeline lanciata via API: stato durevole, sopravvive ai restart."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phd_searcher.database.models.base import Base


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="running")  # running | stopping | stopped | done | failed
    stages: Mapped[list[str]] = mapped_column(JSONB)  # stadi richiesti, in ordine canonico
    stages_done: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)  # stadio -> item processati
    params: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)  # limit/name, riusati da resume
    current_stage: Mapped[str | None] = mapped_column(String(32))
    stage_total: Mapped[int | None]
    stage_done: Mapped[int] = mapped_column(default=0)
    current_label: Mapped[str | None] = mapped_column(String(512))  # ateneo/pagina in lavorazione
    stage_elapsed_seconds: Mapped[float] = mapped_column(default=0.0)  # somma durate unità (avg/ETA a lettura)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
