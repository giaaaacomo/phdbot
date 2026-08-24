"""Request/response types per controllo e stato pipeline. Pure data — no behaviour."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Stage = Literal[
    "universities",
    "euraxess",
    "discovery",
    "schema",
    "scrape",
    "quality",
    "review",
    "evidence",
    "review2",
    "enrich",
    "institutions",
    "index",
]
RunState = Literal["idle", "running", "stopping", "stopped", "done", "failed"]


class PipelineLimits(BaseModel):
    """Budget indipendenti: nessun limite implicito viene propagato ad altri stadi."""

    model_config = ConfigDict(populate_by_name=True)

    universities: int | None = Field(default=None, ge=1)
    discovery: int | None = Field(default=None, ge=1)
    schema_items: int | None = Field(default=None, alias="schema", ge=1)
    scrape: int | None = Field(default=None, ge=1)  # listing source, non posizioni
    quality: int | None = Field(default=None, ge=1)  # listing source sottoposte al quality gate
    review: int | None = Field(default=None, ge=1)  # candidati ambigui sottoposti al tool LLM
    evidence: int | None = Field(default=None, ge=1)  # dettagli recuperati per gli incerti
    review2: int | None = Field(default=None, ge=1)  # incerti con evidenza sottoposti alla review profonda
    enrich: int | None = Field(default=None, ge=1)  # pagine dettaglio
    institutions: int | None = Field(default=None, ge=1)  # entità istituzionali da indicizzare
    index: int | None = Field(default=None, ge=1)  # posizioni


class PipelineStartBody(BaseModel):
    # None = pipeline completa; [] rifiutato (creerebbe una run no-op che seppellisce l'ultima riprendibile)
    stages: list[Stage] | None = Field(default=None, min_length=1)  # ordine di esecuzione sempre canonico
    # Compatibilità per client precedenti; `limits` ha precedenza stadio per stadio.
    limit: int | None = Field(default=None, ge=1)
    limits: PipelineLimits | None = None
    # None = fino alla prima pagina vuota; il limite esplicito serve solo per test/campionamenti.
    max_pages: int | None = Field(default=None, ge=1, le=1500)
    name: str | None = None  # solo atenei il cui nome matcha (ILIKE, come --name)


class DeferredQueueInfo(BaseModel):
    """Lavoro temporaneamente accantonato, separato dal contatore principale."""

    source: str
    total: int
    processed: int
    remaining: int
    cooldown_until: datetime | None = None
    retry_in_seconds: float | None = None
    rate_limit_streak: int = 0


class StageInfo(BaseModel):
    name: str
    total: int | None = None
    done: int = 0
    current: str | None = None  # ateneo/pagina in lavorazione
    avg_seconds: float | None = None
    eta_seconds: float | None = None
    deferred_queue: DeferredQueueInfo | None = None


class PipelineStatus(BaseModel):
    state: RunState
    run_id: int | None = None
    stages: list[str] = []
    stages_done: dict[str, int] = {}
    stages_pending: list[str] = []
    current_stage: StageInfo | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    active_seconds: float | None = None
    error: str | None = None
