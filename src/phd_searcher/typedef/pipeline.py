"""Request/response types per controllo e stato pipeline. Pure data — no behaviour."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Stage = Literal["universities", "euraxess", "discovery", "schema", "scrape", "index"]
RunState = Literal["idle", "running", "stopping", "stopped", "done", "failed"]


class PipelineStartBody(BaseModel):
    # None = pipeline completa; [] rifiutato (creerebbe una run no-op che seppellisce l'ultima riprendibile)
    stages: list[Stage] | None = Field(default=None, min_length=1)  # ordine di esecuzione sempre canonico
    limit: int | None = Field(default=None, ge=1)  # max item per stadio (come --limit della CLI)
    name: str | None = None  # solo atenei il cui nome matcha (ILIKE, come --name)


class StageInfo(BaseModel):
    name: str
    total: int | None = None
    done: int = 0
    current: str | None = None  # ateneo/pagina in lavorazione
    avg_seconds: float | None = None
    eta_seconds: float | None = None


class PipelineStatus(BaseModel):
    state: RunState
    run_id: int | None = None
    stages: list[str] = []
    stages_done: dict[str, int] = {}
    stages_pending: list[str] = []
    current_stage: StageInfo | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
