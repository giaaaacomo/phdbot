"""Progress condiviso runner→stadio: contatori in-memory + write-through su pipeline_runs."""

from __future__ import annotations

from time import monotonic

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.database.models.pipeline_run import PipelineRun


class Progress:
    """Un'istanza per stadio. Senza session_maker/run_id (path CLI) salta le scritture DB.

    should_stop è ri-letto dal DB a ogni unità: lo stop può arrivare da un altro
    worker (segnale = colonna state della riga), non solo dal processo che gira la run.
    """

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession] | None = None,
        run_id: int | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._run_id = run_id
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
                    current_label=self.current,
                    stage_elapsed_seconds=self.elapsed_seconds,
                )
            )
            # rileggo lo state nella stessa sessione: uno stop da un altro worker vive qui
            state = (
                await session.execute(select(PipelineRun.state).where(PipelineRun.id == self._run_id))
            ).scalar_one_or_none()
            await session.commit()
        if state == "stopping":
            self.should_stop = True
