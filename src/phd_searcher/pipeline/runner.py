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
from sqlalchemy import case, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from phd_searcher.database.models.pipeline_run import PipelineRun
from phd_searcher.pipeline import (
    auto_review,
    deep_review,
    discovery,
    enrich,
    euraxess,
    evidence,
    index,
    institutions,
    quality_gate,
    schema_gen,
    scrape,
    universities,
)
from phd_searcher.pipeline.progress import Progress
from phd_searcher.typedef.pipeline import DeferredQueueInfo, PipelineStatus, RunState, StageInfo

StageFn = Callable[..., Coroutine[object, object, int]]

# Ordine canonico di esecuzione (= CLI `all`, senza ingest).
STAGES: dict[str, StageFn] = {
    "universities": universities.run,
    "euraxess": euraxess.run,
    "discovery": discovery.run,
    "schema": schema_gen.run,
    "scrape": scrape.run,
    "quality": quality_gate.run,
    "review": auto_review.run,
    "evidence": evidence.run,
    "review2": deep_review.run,
    "enrich": enrich.run,
    # Publish candidate positions as soon as their hard quality gates are
    # complete. Institution enrichment uses a separate collection and can
    # finish afterwards without delaying the first useful search results.
    "index": index.run,
    "institutions": institutions.run,
}

_LOCK_KEY = 918_273_645  # chiave arbitraria ma fissa dell'advisory lock "pipeline in esecuzione"


class PipelineError(Exception):
    """Richiesta incompatibile con lo stato corrente (le route la mappano su 409)."""


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # colonne naive: asyncpg rifiuta datetime tz-aware


def _utcaware(value: datetime | None) -> datetime | None:
    """Le colonne sono UTC-naive; l'API deve dichiararlo esplicitamente al browser."""
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _checkpoint_int(value: object) -> int:
    try:
        return max(int(cast("int | str", value)), 0)
    except (TypeError, ValueError):
        return 0


def _checkpoint_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _deferred_queue(row: PipelineRun) -> DeferredQueueInfo | None:
    """Deriva lo stato della coda dal checkpoint, anche per run create da versioni precedenti."""
    if row.current_stage not in {"evidence", "enrich"}:
        return None
    raw_checkpoint = row.checkpoints.get(row.current_stage, {})
    if not isinstance(raw_checkpoint, dict):
        return None
    raw_details = raw_checkpoint.get("deferred_details", {})
    details = raw_details if isinstance(raw_details, dict) else {}
    remaining = len(details)
    recorded_total = _checkpoint_int(raw_checkpoint.get("deferred_total"))
    recorded_processed = _checkpoint_int(raw_checkpoint.get("deferred_processed"))
    total = max(recorded_total, recorded_processed + remaining)
    if total == 0:
        return None
    # Se un crash cade fra il commit del dettaglio e il checkpoint, la successiva
    # query elimina comunque l'elemento dalla coda: total - remaining lo recupera.
    processed = min(total, max(recorded_processed, total - remaining))
    cooldown_until = _checkpoint_datetime(raw_checkpoint.get("euraxess_cooldown_until"))
    retry_in = None
    if remaining and cooldown_until is not None:
        retry_in = max((cooldown_until - _utcnow().replace(tzinfo=UTC)).total_seconds(), 0.0)
    return DeferredQueueInfo(
        source="EURAXESS",
        total=total,
        processed=processed,
        remaining=remaining,
        cooldown_until=cooldown_until,
        retry_in_seconds=retry_in,
        rate_limit_streak=_checkpoint_int(raw_checkpoint.get("euraxess_rate_limit_streak")),
    )


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
        await self._reconcile()
        row = await self._latest()
        if row is None:
            raise PipelineError("nothing to resume")
        return await self._resume_row(row)

    async def ensure_scheduled(
        self,
        scheduled_job_id: int,
        stages: list[str],
        params: dict[str, object],
    ) -> int:
        """Start or reattach exactly one durable run for a scheduled job.

        The unique ``scheduled_job_id`` closes the crash window between the
        pipeline INSERT and the scheduler updating its own row. A run stopped
        by an API restart is resumed from its existing checkpoints.
        """
        await self._reconcile()
        row = await self._scheduled_run(scheduled_job_id)
        if row is None:
            scheduled_params = {**params, "_scheduled_job_id": scheduled_job_id}
            return await self.start(stages, scheduled_params)
        if row.state in ("running", "stopping", "done"):
            return row.id
        if row.state == "failed":
            # The persistent scheduler admits only explicit transient failures
            # here and enforces its own bounded retry budget/cooldown.
            return await self._resume_row(row)
        return await self._resume_row(row)

    async def _resume_row(self, row: PipelineRun) -> int:
        selected = set(row.stages)
        ordered_stages = [stage for stage in STAGES if stage in selected]
        pending = [
            stage
            for stage in ordered_stages
            if stage not in row.stages_done
        ]
        if not pending:
            raise PipelineError("last run completed all stages")
        if row.state not in ("stopped", "failed"):
            raise PipelineError("last run is not resumable")
        if not await self._acquire_lock():
            raise PipelineError("pipeline already running")
        try:
            checkpoints = dict(row.checkpoints)
            if row.state == "failed":
                # Un Resume esplicito concede un nuovo budget solo ai retry esauriti;
                # i tentativi ancora in corso dopo uno stop restano invece invariati.
                for stage in pending:
                    # Lo scrape classifica autonomamente un fetch esaurito: rete globale
                    # assente, sorgente permanentemente bloccata o rinvio alla prossima run.
                    # Preservare il budget evita di ripetere gli stessi timeout al Resume.
                    if stage == "scrape":
                        continue
                    raw_checkpoint = checkpoints.get(stage, {})
                    if not isinstance(raw_checkpoint, dict):
                        continue
                    checkpoint = dict(raw_checkpoint)
                    raw_retries = checkpoint.get("retries", {})
                    if not isinstance(raw_retries, dict):
                        continue
                    retries = dict(raw_retries)
                    for key, raw_retry in retries.items():
                        if isinstance(raw_retry, dict) and int(raw_retry.get("attempts", 0)) >= 3:
                            retries[key] = {**raw_retry, "attempts": 0, "next_at": None}
                    checkpoint["retries"] = retries
                    checkpoints[stage] = checkpoint
            now = _utcnow()
            await self._update(
                row.id,
                state="running",
                finished_at=None,
                active_started_at=now,
                active_heartbeat_at=now,
                error=None,
                current_stage=None,
                current_label=None,
                stages=ordered_stages,
                checkpoints=checkpoints,
            )
            # Stessa riga: checkpoint, retry e budget consumato sopravvivono al Resume.
            self._task = asyncio.create_task(self._run(row.id, pending, dict(row.params)))
        except Exception:
            await self._release_lock()
            raise
        return row.id

    async def status(self) -> PipelineStatus:
        await self._reconcile()
        row = await self._latest()
        if row is None:
            return PipelineStatus(state="idle")
        current = None
        if row.current_stage is not None:
            avg = row.stage_elapsed_seconds / row.stage_done if row.stage_done else None
            deferred_queue = _deferred_queue(row)
            eta = (
                max(row.stage_total - row.stage_done, 0) * avg
                if avg is not None and row.stage_total is not None and deferred_queue is None
                else None
            )
            current = StageInfo(
                name=row.current_stage,
                total=row.stage_total,
                done=row.stage_done,
                current=row.current_label,
                avg_seconds=avg,
                eta_seconds=eta,
                deferred_queue=deferred_queue,
            )
        active_seconds = row.active_elapsed_seconds
        if row.active_started_at is not None and row.state in ("running", "stopping"):
            active_seconds += max((_utcnow() - row.active_started_at).total_seconds(), 0.0)
        return PipelineStatus(
            state=cast(RunState, row.state),
            run_id=row.id,
            stages=list(row.stages),
            stages_done=dict(row.stages_done),
            stages_pending=[s for s in row.stages if s not in row.stages_done],
            current_stage=current,
            started_at=_utcaware(row.started_at),
            finished_at=_utcaware(row.finished_at),
            active_seconds=active_seconds,
            error=row.error,
        )

    async def _run(self, run_id: int, stages: list[str], params: dict[str, object]) -> None:
        try:
            for name in stages:
                progress = self._make_progress(run_id, name)
                await self._update(
                    run_id,
                    current_stage=name,
                    stage_total=None,
                    stage_done=0,
                    current_label=None,
                    stage_elapsed_seconds=0.0,
                )
                limits = params.get("limits", {})
                stage_limits = limits if isinstance(limits, dict) else {}
                stage_limit = stage_limits.get(name, params.get("limit"))
                stage_params: dict[str, object] = {
                    "limit": cast("int | None", stage_limit),
                    "name_like": cast("str | None", params.get("name")),
                    "progress": progress,
                }
                if name == "scrape":
                    stage_params["max_pages"] = cast("int | None", params.get("max_pages"))
                result = await STAGES[name](self._container, **stage_params)
                await progress.finish()
                if progress.should_stop:
                    # stadio interrotto: resta fuori da stages_done così resume lo ripete
                    await self._finish_active_interval(run_id, state="stopped", where_state="stopping")
                    return
                await self._merge_stage_done(run_id, name, result)
            # done non-guardato: solo l'owner arriva qui; uno stop arrivato dopo l'ultima lettura
            # di state è volutamente perso (lo stadio era comunque completo)
            await self._finish_active_interval(
                run_id,
                state="done",
                current_stage=None,
                current_label=None,
            )
        except Exception as exc:  # la run fallita resta consultabile e riprendibile via resume
            try:
                await self._finish_active_interval(run_id, state="failed", error=str(exc)[:2000])
            except Exception:  # DB giù: il task muore pulito, reconcile marcherà la run quando il DB torna
                print(f"pipeline runner: cannot persist failure: {exc}")
        finally:
            await self._release_lock()

    def _make_progress(self, run_id: int, stage: str) -> Progress:
        return Progress(self._session_maker, run_id, stage)

    async def _reconcile(self) -> None:
        # una riga running/stopping il cui lock è libero = owner morto (restart/crash): timbrala stopped.
        row = await self._latest()
        if row is None or row.state not in ("running", "stopping"):
            return
        if await self._owner_alive():
            return  # owner vivo (magari un altro worker): non toccare
        # where_state: se la run è terminata nel frattempo, il timbro diventa un no-op
        await self._finish_active_interval(
            row.id,
            state="stopped",
            error="interrupted by restart",
            where_state=row.state,
            interval_end=row.active_heartbeat_at,
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
            now = _utcnow()
            run = PipelineRun(
                scheduled_job_id=cast("int | None", params.get("_scheduled_job_id")),
                stages=stages,
                stages_done={},
                params=params,
                started_at=now,
                active_elapsed_seconds=0.0,
                active_started_at=now,
                active_heartbeat_at=now,
            )
            session.add(run)
            await session.commit()
            return run.id

    async def _finish_active_interval(
        self,
        run_id: int,
        *,
        state: str,
        where_state: str | None = None,
        interval_end: datetime | None = None,
        **values: object,
    ) -> None:
        """Chiude il cronometro corrente senza includere il successivo tempo di inattività."""
        row = await self._get(run_id)
        now = _utcnow()
        elapsed = row.active_elapsed_seconds if row is not None and row.id == run_id else 0.0
        if row is not None and row.id == run_id and row.active_started_at is not None:
            end = interval_end or now
            elapsed += max((end - row.active_started_at).total_seconds(), 0.0)
        await self._update(
            run_id,
            state=state,
            finished_at=now,
            active_elapsed_seconds=elapsed,
            active_started_at=None,
            active_heartbeat_at=None,
            where_state=where_state,
            **values,
        )

    async def _update(self, run_id: int, *, where_state: str | None = None, **values: object) -> None:
        async with self._session_maker() as session:
            stmt = update(PipelineRun).where(PipelineRun.id == run_id).values(**values)
            if where_state is not None:
                stmt = stmt.where(PipelineRun.state == where_state)
            await session.execute(stmt)
            await session.commit()

    async def _latest(self) -> PipelineRun | None:
        async with self._session_maker() as session:
            # Se una vecchia run schedulata viene ripresa dopo una run manuale
            # più recente, lo stato deve comunque mostrare l'unica run attiva.
            active_first = case((PipelineRun.state.in_(("running", "stopping")), 0), else_=1)
            row = await session.execute(
                select(PipelineRun).order_by(active_first, PipelineRun.id.desc()).limit(1)
            )
            return row.scalar_one_or_none()

    async def _get(self, run_id: int) -> PipelineRun | None:
        async with self._session_maker() as session:
            return await session.get(PipelineRun, run_id)

    async def _scheduled_run(self, scheduled_job_id: int) -> PipelineRun | None:
        async with self._session_maker() as session:
            return (
                await session.execute(
                    select(PipelineRun).where(PipelineRun.scheduled_job_id == scheduled_job_id).limit(1)
                )
            ).scalar_one_or_none()

    async def _merge_stage_done(self, run_id: int, stage: str, count: int) -> None:
        async with self._session_maker() as session:
            row = await session.get(PipelineRun, run_id)
            if row is not None:
                row.stages_done = {**row.stages_done, stage: count}  # riassegnazione: il change tracking la vede
                await session.commit()
