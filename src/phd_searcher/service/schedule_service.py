"""Restart-safe one-shot scheduler for pipelines and saved macros."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from injector import inject, singleton
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select

from phd_searcher.database.models.macro import MacroRun, SavedMacro
from phd_searcher.database.models.pipeline_run import PipelineRun
from phd_searcher.database.models.scheduled_job import ScheduledJob
from phd_searcher.pipeline.runner import STAGES, PipelineError, PipelineRunner
from phd_searcher.service.macro_service import MacroService
from phd_searcher.typedef.pipeline import PipelineStartBody
from phd_searcher.typedef.schedule import ScheduleCreate, ScheduleState, ScheduleTarget, ScheduleView

ROME = ZoneInfo("Europe/Rome")
_ACTIVE_STATES = ("scheduled", "waiting_pipeline", "starting", "running")
_CLAIM_SECONDS = 120
_BUSY_RETRY_SECONDS = 30
_POLL_SECONDS = 10.0
_MAX_SCHEDULED_PIPELINE_ATTEMPTS = 4
_TRANSIENT_PIPELINE_RETRY_SECONDS = 300
_RATE_LIMIT_PIPELINE_RETRY_SECONDS = 900
_TRANSIENT_PIPELINE_ERROR_MARKERS = (
    "429",
    "bad gateway",
    "connection refused",
    "connection reset",
    "gateway timeout",
    "ollama",
    "rate limit",
    "retries exhausted",
    "server disconnected",
    "service unavailable",
    "temporarily unavailable",
    "timed out",
    "timeout",
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _scheduled_pipeline_retry_delay(error: object, attempts: int) -> int | None:
    """Return a bounded unattended retry delay for explicit transient failures."""
    if attempts >= _MAX_SCHEDULED_PIPELINE_ATTEMPTS or not isinstance(error, str):
        return None
    normalized = error.casefold()
    if not any(marker in normalized for marker in _TRANSIENT_PIPELINE_ERROR_MARKERS):
        return None
    if "429" in normalized or "rate limit" in normalized:
        return _RATE_LIMIT_PIPELINE_RETRY_SECONDS
    return _TRANSIENT_PIPELINE_RETRY_SECONDS


def _utcaware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _to_utc_naive(value: datetime, timezone: str = "Europe/Rome") -> datetime:
    """Resolve a local wall-clock time without silently guessing across DST."""
    zone = ZoneInfo(timezone)
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)

    fold0 = value.replace(tzinfo=zone, fold=0)
    fold1 = value.replace(tzinfo=zone, fold=1)

    def round_trips(candidate: datetime) -> bool:
        back = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        return back == value

    valid0 = round_trips(fold0)
    valid1 = round_trips(fold1)
    if not valid0 and not valid1:
        raise ValueError("the selected Europe/Rome time does not exist because of the DST transition")
    if valid0 and valid1 and fold0.utcoffset() != fold1.utcoffset():
        raise ValueError("the selected Europe/Rome time is ambiguous because of the DST transition")
    resolved = fold0 if valid0 else fold1
    return resolved.astimezone(UTC).replace(tzinfo=None)


def _due_jobs_statement(now: datetime) -> Select[tuple[ScheduledJob]]:
    due = or_(
        and_(ScheduledJob.state == "scheduled", ScheduledJob.run_at <= now),
        and_(
            ScheduledJob.state == "waiting_pipeline",
            or_(ScheduledJob.next_attempt_at.is_(None), ScheduledJob.next_attempt_at <= now),
        ),
        and_(
            ScheduledJob.state == "starting",
            or_(ScheduledJob.lease_until.is_(None), ScheduledJob.lease_until <= now),
        ),
    )
    return (
        select(ScheduledJob)
        .where(due)
        .order_by(ScheduledJob.run_at, ScheduledJob.id)
        .with_for_update(skip_locked=True)
        .limit(8)
    )


@singleton
class ScheduleService:
    """Database-backed poller; no shell, browser or Codex process needs to wait."""

    @inject
    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        pipeline: PipelineRunner,
        macros: MacroService,
    ) -> None:
        self._session_maker = session_maker
        self._pipeline = pipeline
        self._macros = macros
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    @staticmethod
    def _view(job: ScheduledJob) -> ScheduleView:
        run_at = cast(datetime, _utcaware(job.run_at))
        return ScheduleView(
            id=job.id,
            target=cast(ScheduleTarget, job.target),
            state=cast(ScheduleState, job.state),
            run_at=run_at,
            local_run_at=run_at.astimezone(ROME),
            timezone="Europe/Rome",
            pipeline=PipelineStartBody.model_validate(job.payload) if job.target == "pipeline" else None,
            macro_id=job.macro_id,
            pipeline_run_id=job.pipeline_run_id,
            macro_run_id=job.macro_run_id,
            attempts=job.attempts,
            next_attempt_at=_utcaware(job.next_attempt_at),
            error=job.error,
            created_at=cast(datetime, _utcaware(job.created_at)),
            started_at=_utcaware(job.started_at),
            finished_at=_utcaware(job.finished_at),
        )

    async def create(self, body: ScheduleCreate) -> ScheduleView:
        run_at = _to_utc_naive(body.run_at, body.timezone)
        if run_at <= _utcnow():
            raise ValueError("scheduled time must be in the future")
        async with self._session_maker() as session:
            if body.target == "macro":
                macro = await session.get(SavedMacro, body.macro_id)
                if macro is None:
                    raise LookupError("macro not found")
                if not macro.enabled:
                    raise ValueError("macro is disabled")
            payload = (
                body.pipeline.model_dump(mode="json", exclude_none=True, by_alias=True)
                if body.pipeline is not None
                else {}
            )
            job = ScheduledJob(
                target=body.target,
                state="scheduled",
                run_at=run_at,
                timezone=body.timezone,
                payload=payload,
                macro_id=body.macro_id,
                attempts=0,
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)
            view = self._view(job)
        self._wake.set()
        return view

    async def list(self, *, active_only: bool = False, limit: int = 100) -> list[ScheduleView]:
        async with self._session_maker() as session:
            stmt = select(ScheduledJob)
            if active_only:
                stmt = stmt.where(ScheduledJob.state.in_(_ACTIVE_STATES))
            rows = (
                await session.execute(stmt.order_by(ScheduledJob.id.desc()).limit(limit))
            ).scalars().all()
            return [self._view(row) for row in rows]

    async def get(self, job_id: int) -> ScheduleView | None:
        async with self._session_maker() as session:
            job = await session.get(ScheduledJob, job_id)
            return self._view(job) if job is not None else None

    async def cancel(self, job_id: int) -> ScheduleView | None:
        now = _utcnow()
        async with self._session_maker() as session:
            result = await session.execute(
                update(ScheduledJob)
                .where(
                    ScheduledJob.id == job_id,
                    ScheduledJob.state.in_(("scheduled", "waiting_pipeline")),
                )
                .values(
                    state="cancelled",
                    lease_until=None,
                    next_attempt_at=None,
                    finished_at=now,
                    error=None,
                )
                .returning(ScheduledJob.id)
            )
            cancelled_id = result.scalar_one_or_none()
            await session.commit()
            job = await session.get(ScheduledJob, job_id)
            if job is None:
                return None
            if cancelled_id is None:
                raise ValueError("only a schedule that has not started can be cancelled")
            return self._view(job)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="phdbot-scheduler")

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Un errore transitorio del DB non uccide il pianificatore.
                print(f"scheduler tick failed: {exc}")
            self._wake.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=_POLL_SECONDS)

    async def tick(self) -> None:
        await self._reconcile_running()
        for job_id in await self._claim_due():
            await self._dispatch(job_id)

    async def _claim_due(self) -> Sequence[int]:
        now = _utcnow()
        async with self._session_maker() as session:
            jobs = (await session.execute(_due_jobs_statement(now))).scalars().all()
            for job in jobs:
                job.state = "starting"
                job.attempts += 1
                job.lease_until = now + timedelta(seconds=_CLAIM_SECONDS)
                job.next_attempt_at = None
            await session.commit()
            return [job.id for job in jobs]

    async def _dispatch(self, job_id: int) -> None:
        async with self._session_maker() as session:
            job = await session.get(ScheduledJob, job_id)
            if job is None or job.state != "starting":
                return
            target = job.target
            payload = dict(job.payload)
            macro_id = job.macro_id

        if target == "pipeline":
            body = PipelineStartBody.model_validate(payload)
            stages = [name for name in STAGES if body.stages is None or name in body.stages]
            limits = body.limits.model_dump(exclude_none=True, by_alias=True) if body.limits else {}
            params: dict[str, object] = {
                "limit": body.limit,
                "limits": limits,
                "max_pages": body.max_pages,
                "name": body.name,
            }
            try:
                pipeline_run_id = await self._pipeline.ensure_scheduled(job_id, stages, params)
            except PipelineError as exc:
                if "already running" in str(exc):
                    await self._defer(job_id, "waiting for the active pipeline to finish")
                else:
                    await self._fail(job_id, str(exc))
                return
            await self._mark_running(job_id, pipeline_run_id=pipeline_run_id)
            return

        if macro_id is None:
            await self._fail(job_id, "scheduled macro has no macro_id")
            return
        try:
            macro_run = await self._macros.run(macro_id, scheduled_job_id=job_id)
        except ValueError as exc:
            await self._fail(job_id, str(exc))
            return
        if macro_run is None:
            await self._fail(job_id, "macro not found")
            return
        await self._mark_running(job_id, macro_run_id=macro_run.id)

    async def _mark_running(
        self,
        job_id: int,
        *,
        pipeline_run_id: int | None = None,
        macro_run_id: int | None = None,
    ) -> None:
        now = _utcnow()
        async with self._session_maker() as session:
            await session.execute(
                update(ScheduledJob)
                .where(ScheduledJob.id == job_id, ScheduledJob.state == "starting")
                .values(
                    state="running",
                    pipeline_run_id=pipeline_run_id,
                    macro_run_id=macro_run_id,
                    started_at=func.coalesce(ScheduledJob.started_at, now),
                    lease_until=None,
                    error=None,
                )
            )
            await session.commit()

    async def _defer(self, job_id: int, reason: str) -> None:
        now = _utcnow()
        async with self._session_maker() as session:
            await session.execute(
                update(ScheduledJob)
                .where(ScheduledJob.id == job_id, ScheduledJob.state == "starting")
                .values(
                    state="waiting_pipeline",
                    next_attempt_at=now + timedelta(seconds=_BUSY_RETRY_SECONDS),
                    lease_until=None,
                    error=reason[:2_000],
                )
            )
            await session.commit()

    async def _fail(self, job_id: int, error: str) -> None:
        async with self._session_maker() as session:
            await session.execute(
                update(ScheduledJob)
                .where(ScheduledJob.id == job_id)
                .values(
                    state="failed",
                    lease_until=None,
                    next_attempt_at=None,
                    error=error[:2_000],
                    finished_at=_utcnow(),
                )
            )
            await session.commit()

    async def _reconcile_running(self) -> None:
        async with self._session_maker() as session:
            jobs = (
                await session.execute(
                    select(ScheduledJob).where(ScheduledJob.state == "running").order_by(ScheduledJob.id)
                )
            ).scalars().all()
            targets = [
                (job.id, job.target, job.pipeline_run_id, job.macro_run_id, job.attempts)
                for job in jobs
            ]
        if any(target == "pipeline" for _, target, _, _, _ in targets):
            # Riconcilia l'advisory lock dopo un eventuale riavvio dell'API.
            await self._pipeline.status()
        for job_id, target, pipeline_run_id, macro_run_id, attempts in targets:
            if target == "pipeline":
                await self._reconcile_pipeline(job_id, pipeline_run_id, attempts)
            else:
                await self._reconcile_macro(job_id, macro_run_id)

    async def _reconcile_pipeline(
        self,
        job_id: int,
        pipeline_run_id: int | None,
        attempts: int,
    ) -> None:
        async with self._session_maker() as session:
            row = await session.get(PipelineRun, pipeline_run_id) if pipeline_run_id is not None else None
            if row is None:
                row = (
                    await session.execute(
                        select(PipelineRun).where(PipelineRun.scheduled_job_id == job_id).limit(1)
                    )
                ).scalar_one_or_none()
            if row is None:
                await session.execute(
                    update(ScheduledJob)
                    .where(ScheduledJob.id == job_id, ScheduledJob.state == "running")
                    .values(state="starting", lease_until=_utcnow(), pipeline_run_id=None)
                )
            elif row.state == "done":
                await self._finish_in_session(session, job_id, "done")
            elif row.state == "failed":
                retry_delay = _scheduled_pipeline_retry_delay(row.error, attempts)
                if retry_delay is None:
                    await self._finish_in_session(
                        session,
                        job_id,
                        "failed",
                        row.error or "pipeline failed",
                    )
                else:
                    await session.execute(
                        update(ScheduledJob)
                        .where(
                            ScheduledJob.id == job_id,
                            ScheduledJob.state == "running",
                        )
                        .values(
                            state="waiting_pipeline",
                            next_attempt_at=_utcnow() + timedelta(seconds=retry_delay),
                            lease_until=None,
                            error=(
                                f"automatic resume in {retry_delay}s after transient failure: "
                                f"{row.error or 'unknown error'}"
                            )[:2_000],
                        )
                    )
            elif row.state == "stopped":
                if row.error == "interrupted by restart":
                    await session.execute(
                        update(ScheduledJob)
                        .where(ScheduledJob.id == job_id, ScheduledJob.state == "running")
                        .values(
                            state="waiting_pipeline",
                            next_attempt_at=_utcnow(),
                            pipeline_run_id=row.id,
                            error="resuming after API restart",
                        )
                    )
                else:
                    await self._finish_in_session(
                        session,
                        job_id,
                        "failed",
                        "pipeline was stopped; its checkpoint is preserved for manual resume",
                    )
            else:
                await session.execute(
                    update(ScheduledJob)
                    .where(ScheduledJob.id == job_id)
                    .values(pipeline_run_id=row.id)
                )
            await session.commit()

    async def _reconcile_macro(self, job_id: int, macro_run_id: int | None) -> None:
        async with self._session_maker() as session:
            row = await session.get(MacroRun, macro_run_id) if macro_run_id is not None else None
            if row is None:
                row = (
                    await session.execute(
                        select(MacroRun).where(MacroRun.scheduled_job_id == job_id).limit(1)
                    )
                ).scalar_one_or_none()
            if row is None:
                await session.execute(
                    update(ScheduledJob)
                    .where(ScheduledJob.id == job_id, ScheduledJob.state == "running")
                    .values(state="starting", lease_until=_utcnow(), macro_run_id=None)
                )
            elif row.state == "done":
                await self._finish_in_session(session, job_id, "done")
            elif row.state == "failed":
                await self._finish_in_session(session, job_id, "failed", row.error or "macro failed")
            else:
                await session.execute(
                    update(ScheduledJob).where(ScheduledJob.id == job_id).values(macro_run_id=row.id)
                )
            await session.commit()

    @staticmethod
    async def _finish_in_session(
        session: AsyncSession,
        job_id: int,
        state: str,
        error: str | None = None,
    ) -> None:
        await session.execute(
            update(ScheduledJob)
            .where(ScheduledJob.id == job_id, ScheduledJob.state == "running")
            .values(
                state=state,
                error=error,
                lease_until=None,
                next_attempt_at=None,
                finished_at=_utcnow(),
            )
        )
