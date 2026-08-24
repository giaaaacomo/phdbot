"""Una riga per run della pipeline lanciata via API: stato durevole, sopravvive ai restart."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phd_searcher.database.models.base import Base


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Chiave idempotente per gli avvii differiti. Una schedule non può creare
    # due run neppure se l'API cade fra l'INSERT e l'aggiornamento del job.
    scheduled_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_jobs.id", ondelete="SET NULL"), unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(16), default="running")  # running | stopping | stopped | done | failed
    stages: Mapped[list[str]] = mapped_column(JSONB)  # stadi richiesti, in ordine canonico
    stages_done: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)  # stadio -> item processati
    params: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)  # limit/name, riusati da resume
    # Cursori e retry per stadio. Rimangono sulla stessa run durante Resume.
    checkpoints: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    current_stage: Mapped[str | None] = mapped_column(String(32))
    stage_total: Mapped[int | None]
    stage_done: Mapped[int] = mapped_column(default=0)
    current_label: Mapped[str | None] = mapped_column(String(512))  # ateneo/pagina in lavorazione
    stage_elapsed_seconds: Mapped[float] = mapped_column(default=0.0)  # somma durate unità (avg/ETA a lettura)
    # Cronometro della run: accumula soltanto gli intervalli running/stopping.
    # Al Resume parte un nuovo intervallo senza includere il tempo trascorso da stopped/failed.
    active_elapsed_seconds: Mapped[float] = mapped_column(default=0.0)
    active_started_at: Mapped[datetime | None]
    active_heartbeat_at: Mapped[datetime | None]  # ultimo segnale del worker, usato dopo crash/power loss
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
