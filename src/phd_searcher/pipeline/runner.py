"""Esecuzione pipeline: al più una run attiva NELL'INTERO cluster, stato durevole su pipeline_runs.

Il controllo è mediato da Postgres, non dalla memoria di processo, così più worker
(hypercorn --workers N) restano coerenti:
- ownership = advisory lock di sessione `pg_advisory_lock(_LOCK_KEY)` tenuto su una
  connessione dedicata per tutta la durata della run. È atomico (niente doppia start
  cross-process) e se il worker muore Postgres lo rilascia da solo (→ run orfana rilevabile).
- stop = colonna `state` della riga: qualunque worker scrive 'stopping', il worker che
  possiede la run lo legge a ogni unità (via Progress) e si ferma tra un'unità e l'altra.
- reconcile = prova a prendere il lock: riga running/stopping + lock libero = owner morto.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import cast

from injector import Injector, inject, singleton
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from phd_searcher.database.models.pipeline_run import PipelineRun
from phd_searcher.pipeline import discovery, euraxess, index, schema_gen, scrape, universities
from phd_searcher.pipeline.progress import Progress
from phd_searcher.typedef.pipeline import PipelineStatus, RunState, StageInfo

StageFn = Callable[..., Coroutine[object, object, int]]

# Ordine canonico di esecuzione (= CLI `all`, senza ingest).
STAGES: dict[str, StageFn] = {
    "universities": universities.run,
    "euraxess": euraxess.run,
    "discovery": discovery.run,
    "schema": schema_gen.run,
    "scrape": scrape.run,
    "index": index.run,
}

_LOCK_KEY = 918_273_645  # chiave arbitraria ma fissa dell'advisory lock "pipeline in esecuzione"


class PipelineError(Exception):
    """Richiesta incompatibile con lo stato corrente (le route la mappano su 409)."""


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # colonne naive: asyncpg rifiuta datetime tz-aware


@singleton
class PipelineRunner:
    """Ownership e stop vivono su Postgres (advisory lock + colonna state): safe multi-worker."""

    @inject
    def __init__(
        self,
        container: Injector,
        session_maker: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
    ) -> None:
        self._container = container
        self._session_maker = session_maker
        self._engine = engine
        self._task: asyncio.Task[None] | None = None  # solo per tenere un riferimento vivo al task
        self._lock_conn: AsyncConnection | None = None  # connessione che tiene l'advisory lock

    async def start(self, stages: list[str], params: dict[str, object]) -> int:
        await self._reconcile()
        if not await self._acquire_lock():  # atomico e cross-process: chi lo prende possiede la run
            raise PipelineError("pipeline already running")
        try:
            run_id = await self._insert_run(stages, params)
            self._task = asyncio.create_task(
                self._run(run_id, stages, params)
            )  # dentro il try: rilascia il lock se fallisce
        except Exception:
            await self._release_lock()
            raise
        return run_id

    async def stop(self) -> int:
        await self._reconcile()
        row = await self._latest()
        if row is None or row.state not in ("running", "stopping"):
            raise PipelineError("no run in progress")
        # segnale via DB: il worker che possiede la run lo legge alla prossima unità
        await self._update(row.id, state="stopping", where_state="running")
        return row.id

    async def resume(self) -> int:
        row = await self._latest()  # niente reconcile qui: start() sotto lo fa e pulisce eventuali orfane
        if row is None:
            raise PipelineError("nothing to resume")
        pending = [s for s in row.stages if s not in row.stages_done]
        if not pending:
            raise PipelineError("last run completed all stages")
        return await self.start(pending, dict(row.params))  # start 409-a se un'altra run è attiva

    async def status(self) -> PipelineStatus:
        await self._reconcile()
        row = await self._latest()
        if row is None:
            return PipelineStatus(state="idle")
        current = None
        if row.current_stage is not None:
            avg = row.stage_elapsed_seconds / row.stage_done if row.stage_done else None
            eta = (
                max(row.stage_total - row.stage_done, 0) * avg
                if avg is not None and row.stage_total is not None
                else None
            )
            current = StageInfo(
                name=row.current_stage,
                total=row.stage_total,
                done=row.stage_done,
                current=row.current_label,
                avg_seconds=avg,
                eta_seconds=eta,
            )
        return PipelineStatus(
            state=cast(RunState, row.state),
            run_id=row.id,
            stages=list(row.stages),
            stages_done=dict(row.stages_done),
            stages_pending=[s for s in row.stages if s not in row.stages_done],
            current_stage=current,
            started_at=row.started_at,
            finished_at=row.finished_at,
            error=row.error,
        )

    async def _run(self, run_id: int, stages: list[str], params: dict[str, object]) -> None:
        try:
            for name in stages:
                progress = self._make_progress(run_id)
                await self._update(
                    run_id,
                    current_stage=name,
                    stage_total=None,
                    stage_done=0,
                    current_label=None,
                    stage_elapsed_seconds=0.0,
                )
                result = await STAGES[name](
                    self._container,
                    limit=cast("int | None", params.get("limit")),
                    name_like=cast("str | None", params.get("name")),
                    progress=progress,
                )
                await progress.finish()
                if progress.should_stop:
                    # stadio interrotto: resta fuori da stages_done così resume lo ripete
                    await self._update(run_id, state="stopped", finished_at=_utcnow(), where_state="stopping")
                    return
                await self._merge_stage_done(run_id, name, result)
            # done non-guardato: solo l'owner arriva qui; uno stop arrivato dopo l'ultima lettura
            # di state è volutamente perso (lo stadio era comunque completo)
            await self._update(run_id, state="done", finished_at=_utcnow(), current_stage=None, current_label=None)
        except Exception as exc:  # la run fallita resta consultabile e riprendibile via resume
            try:
                await self._update(run_id, state="failed", error=str(exc)[:2000], finished_at=_utcnow())
            except Exception:  # DB giù: il task muore pulito, reconcile marcherà la run quando il DB torna
                print(f"pipeline runner: cannot persist failure: {exc}")
        finally:
            await self._release_lock()

    def _make_progress(self, run_id: int) -> Progress:
        return Progress(self._session_maker, run_id)

    async def _reconcile(self) -> None:
        # una riga running/stopping il cui lock è libero = owner morto (restart/crash): timbrala stopped.
        row = await self._latest()
        if row is None or row.state not in ("running", "stopping"):
            return
        if await self._owner_alive():
            return  # owner vivo (magari un altro worker): non toccare
        # where_state: se la run è terminata nel frattempo, il timbro diventa un no-op
        await self._update(
            row.id,
            state="stopped",
            error="interrupted by restart",
            finished_at=_utcnow(),
            where_state=row.state,
        )

    # --- advisory lock (i test unit sovrascrivono questi tre + i quattro di persistenza) ---

    async def _acquire_lock(self) -> bool:
        """Prende il lock e tiene aperta la connessione (= possiede la run). False se già preso."""
        conn = await self._engine.connect()
        try:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            got = bool((await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY})).scalar())
        except BaseException:  # niente connessione orfana (= backend/lock trapelato) su errore transitorio
            await conn.close()
            raise
        if not got:
            await conn.close()
            return False
        self._lock_conn = conn
        return True

    async def _release_lock(self) -> None:
        conn, self._lock_conn = self._lock_conn, None
        if conn is None:
            return
        try:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
        finally:
            await conn.close()

    async def _owner_alive(self) -> bool:
        """True se qualcuno (anche un altro worker) tiene il lock: il try fallisce."""
        async with self._engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            got = bool((await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY})).scalar())
            if got:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            return not got

    # --- persistenza (i test unit sovrascrivono solo questi quattro metodi) ---

    async def _insert_run(self, stages: list[str], params: dict[str, object]) -> int:
        async with self._session_maker() as session:
            # started_at esplicito UTC-naive: coerente con finished_at (_utcnow), non wall time del DB
            run = PipelineRun(stages=stages, stages_done={}, params=params, started_at=_utcnow())
            session.add(run)
            await session.commit()
            return run.id

    async def _update(self, run_id: int, *, where_state: str | None = None, **values: object) -> None:
        async with self._session_maker() as session:
            stmt = update(PipelineRun).where(PipelineRun.id == run_id).values(**values)
            if where_state is not None:
                stmt = stmt.where(PipelineRun.state == where_state)
            await session.execute(stmt)
            await session.commit()

    async def _latest(self) -> PipelineRun | None:
        async with self._session_maker() as session:
            row = await session.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1))
            return row.scalar_one_or_none()

    async def _merge_stage_done(self, run_id: int, stage: str, count: int) -> None:
        async with self._session_maker() as session:
            row = await session.get(PipelineRun, run_id)
            if row is not None:
                row.stages_done = {**row.stages_done, stage: count}  # riassegnazione: il change tracking la vede
                await session.commit()
