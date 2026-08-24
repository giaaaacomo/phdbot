"""Progress condiviso runner→stadio: contatori in-memory + write-through su pipeline_runs."""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.database.models.pipeline_run import PipelineRun

_CURRENT_LABEL_MAX_LENGTH = 512


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _persisted_label(label: str | None) -> str | None:
    """Adatta soltanto l'etichetta di stato al limite del control plane."""
    if label is None or len(label) <= _CURRENT_LABEL_MAX_LENGTH:
        return label
    return f"{label[: _CURRENT_LABEL_MAX_LENGTH - 1]}…"


class Progress:
    """Un'istanza per stadio. Senza session_maker/run_id (path CLI) salta le scritture DB.

    should_stop è ri-letto dal DB a ogni unità: lo stop può arrivare da un altro
    worker (segnale = colonna state della riga), non solo dal processo che gira la run.
    """

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession] | None = None,
        run_id: int | None = None,
        stage: str | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._run_id = run_id
        self._stage = stage
        self._checkpoint: dict[str, object] = {}
        self._checkpoint_loaded = False
        self.should_stop = False
        self.total: int | None = None
        self.done = 0
        self.current: str | None = None
        self.elapsed_seconds = 0.0
        self._mark: float | None = None

    @property
    def avg_seconds(self) -> float | None:
        return self.elapsed_seconds / self.done if self.done else None

    @property
    def run_id(self) -> int | None:
        """Run proprietaria, esposta agli stadi che salvano audit append-only."""
        return self._run_id

    @property
    def stage(self) -> str | None:
        return self._stage

    @property
    def eta_seconds(self) -> float | None:
        avg = self.avg_seconds
        if avg is None or self.total is None:
            return None
        return max(self.total - self.done, 0) * avg

    async def begin(self, total: int) -> None:
        """Lo stadio dichiara quante unità processerà (dopo la sua query)."""
        self.total = total
        self._mark = monotonic()
        await self._persist()

    async def tick(self, label: str) -> None:
        """A inizio unità: chiude la precedente (durata, done) e imposta la corrente."""
        now = monotonic()
        if self.current is not None and self._mark is not None:
            self.elapsed_seconds += now - self._mark
            self.done += 1
        self._mark = now
        self.current = label
        await self._persist()

    async def finish(self) -> None:
        """Fine stadio (chiamata dal runner): l'ultima unità conta come completata."""
        if self.current is not None and self._mark is not None:
            self.elapsed_seconds += monotonic() - self._mark
            self.done += 1
        self.current = None
        await self._persist()

    async def check_stop(self) -> None:
        """Rilegge il segnale di stop senza avanzare contatori o durata dell'unità."""
        await self._persist()

    async def load_checkpoint(self) -> dict[str, object]:
        """Carica una volta il cursore durevole dello stadio corrente."""
        if self._checkpoint_loaded:
            return dict(self._checkpoint)
        if self._session_maker is not None and self._run_id is not None and self._stage is not None:
            async with self._session_maker() as session:
                row = await session.get(PipelineRun, self._run_id)
                if row is not None:
                    value = row.checkpoints.get(self._stage, {})
                    if isinstance(value, dict):
                        self._checkpoint = dict(value)
        self._checkpoint_loaded = True
        return dict(self._checkpoint)

    async def load_stage_checkpoint(self, stage: str) -> dict[str, object]:
        """Legge il checkpoint di un altro stadio della stessa run.

        Serve a collegare coorti durevoli fra review, evidence e review2 senza
        affidarsi all'ordine globale degli ID. In esecuzione CLI non mediata dal
        runner non esiste una coorte e viene restituito un dizionario vuoto.
        """
        if self._session_maker is None or self._run_id is None:
            return {}
        async with self._session_maker() as session:
            row = await session.get(PipelineRun, self._run_id)
            if row is None:
                return {}
            value = row.checkpoints.get(stage, {})
            return dict(value) if isinstance(value, dict) else {}

    async def save_checkpoint(self, **values: object) -> None:
        """Merge atomico del checkpoint dello stadio; chiamare dopo il commit del lavoro."""
        await self.load_checkpoint()
        self._checkpoint.update(values)
        if self._session_maker is None or self._run_id is None or self._stage is None:
            return
        async with self._session_maker() as session:
            row = await session.get(PipelineRun, self._run_id)
            if row is None:
                return
            row.checkpoints = {**row.checkpoints, self._stage: dict(self._checkpoint)}
            await session.commit()

    async def _persist(self) -> None:
        if self._session_maker is None or self._run_id is None:
            return
        async with self._session_maker() as session:  # sessione propria: mai quella dello stadio
            await session.execute(
                update(PipelineRun)
                .where(PipelineRun.id == self._run_id)
                .values(
                    stage_total=self.total,
                    stage_done=self.done,
                    current_label=_persisted_label(self.current),
                    stage_elapsed_seconds=self.elapsed_seconds,
                    active_heartbeat_at=_utcnow(),
                )
            )
            # rileggo lo state nella stessa sessione: uno stop da un altro worker vive qui
            state = (
                await session.execute(select(PipelineRun.state).where(PipelineRun.id == self._run_id))
            ).scalar_one_or_none()
            await session.commit()
        if state == "stopping":
            self.should_stop = True
