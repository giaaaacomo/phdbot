"""Durable orchestration for saved refresh/search/export macros."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

from injector import inject, singleton
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from phd_searcher.database.models.macro import MacroRun, SavedMacro
from phd_searcher.database.models.pipeline_run import PipelineRun
from phd_searcher.pipeline.runner import STAGES, PipelineRunner
from phd_searcher.service.export_service import ExportService
from phd_searcher.typedef.macro import MacroCreate, MacroRunState, MacroRunView, MacroView
from phd_searcher.typedef.pipeline import PipelineStartBody
from phd_searcher.typedef.search import ExportFormat, SearchBody


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


_MACRO_LOCK_NAMESPACE = 7_419_233


@singleton
class MacroService:
    @inject
    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        pipeline: PipelineRunner,
        exports: ExportService,
    ) -> None:
        self._session_maker = session_maker
        self._engine = engine
        self._pipeline = pipeline
        self._exports = exports
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._execution_lock = asyncio.Lock()

    @staticmethod
    def _view(macro: SavedMacro) -> MacroView:
        return MacroView(
            id=macro.id,
            name=macro.name,
            description=macro.description,
            enabled=macro.enabled,
            refresh=macro.refresh,
            pipeline=PipelineStartBody.model_validate(macro.pipeline_params),
            search=SearchBody.model_validate(macro.search_body),
            export_formats=[cast(ExportFormat, value) for value in macro.export_formats],
            destination=macro.destination,
            created_at=macro.created_at,
            updated_at=macro.updated_at,
        )

    @staticmethod
    def _run_view(run: MacroRun) -> MacroRunView:
        return MacroRunView(
            id=run.id,
            macro_id=run.macro_id,
            scheduled_job_id=run.scheduled_job_id,
            state=cast(MacroRunState, run.state),
            current_step=run.current_step,
            pipeline_run_id=run.pipeline_run_id,
            outputs=list(run.outputs),
            error=run.error,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )

    async def list(self) -> list[MacroView]:
        async with self._session_maker() as session:
            rows = (await session.execute(select(SavedMacro).order_by(SavedMacro.name))).scalars().all()
            return [self._view(row) for row in rows]

    async def create(self, body: MacroCreate) -> MacroView:
        async with self._session_maker() as session:
            existing = (
                await session.execute(select(SavedMacro).where(SavedMacro.name == body.name.strip()))
            ).scalar_one_or_none()
            if existing is not None:
                raise ValueError("a macro with this name already exists")
            macro = SavedMacro(
                name=body.name.strip(),
                description=body.description,
                refresh=body.refresh,
                pipeline_params=body.pipeline.model_dump(mode="json", exclude_none=True, by_alias=True),
                search_body=body.search.model_dump(mode="json"),
                export_formats=list(body.export_formats),
                destination=body.destination,
            )
            session.add(macro)
            await session.commit()
            await session.refresh(macro)
            return self._view(macro)

    async def run(self, macro_id: int, *, scheduled_job_id: int | None = None) -> MacroRunView | None:
        async with self._session_maker() as session:
            if scheduled_job_id is not None:
                existing = (
                    await session.execute(
                        select(MacroRun).where(MacroRun.scheduled_job_id == scheduled_job_id).limit(1)
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    view = self._run_view(existing)
                    if existing.state not in ("done", "failed"):
                        self._spawn(existing.id)
                    return view
            macro = await session.get(SavedMacro, macro_id)
            if macro is None:
                return None
            if not macro.enabled:
                raise ValueError("macro is disabled")
            run = MacroRun(
                macro_id=macro_id,
                scheduled_job_id=scheduled_job_id,
                state="queued",
                outputs=[],
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            view = self._run_view(run)
        self._spawn(view.id)
        return view

    async def get_run(self, run_id: int) -> MacroRunView | None:
        async with self._session_maker() as session:
            run = await session.get(MacroRun, run_id)
            return self._run_view(run) if run is not None else None

    async def recover(self) -> None:
        async with self._session_maker() as session:
            run_ids = (
                await session.execute(
                    select(MacroRun.id).where(
                        MacroRun.state.in_(("queued", "waiting_pipeline", "running_pipeline", "exporting"))
                    )
                )
            ).scalars().all()
        for run_id in run_ids:
            self._spawn(run_id)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _spawn(self, run_id: int) -> None:
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._execute(run_id))
        self._tasks[run_id] = task

        def forget(_task: asyncio.Task[None]) -> None:
            self._tasks.pop(run_id, None)

        task.add_done_callback(forget)

    async def _update_run(self, run_id: int, **values: object) -> None:
        async with self._session_maker() as session:
            run = await session.get(MacroRun, run_id)
            if run is None:
                return
            for key, value in values.items():
                setattr(run, key, value)
            await session.commit()

    async def _load(self, run_id: int) -> tuple[MacroRun, SavedMacro] | None:
        async with self._session_maker() as session:
            row = (
                await session.execute(
                    select(MacroRun, SavedMacro)
                    .join(SavedMacro, MacroRun.macro_id == SavedMacro.id)
                    .where(MacroRun.id == run_id)
                )
            ).first()
            if row is None:
                return None
            run, macro = row
            session.expunge(run)
            session.expunge(macro)
            return run, macro

    async def _pipeline_state(self, pipeline_run_id: int) -> str | None:
        async with self._session_maker() as session:
            return cast(
                "str | None",
                await session.scalar(select(PipelineRun.state).where(PipelineRun.id == pipeline_run_id)),
            )

    async def _wait_for_pipeline(self, run_id: int, pipeline_run_id: int) -> None:
        while True:
            # Riconcilia un eventuale owner morto dopo un riavvio dell'API.
            await self._pipeline.status()
            state = await self._pipeline_state(pipeline_run_id)
            if state == "done":
                return
            if state in ("stopped", "failed"):
                resumed = await self._pipeline.resume()
                if resumed != pipeline_run_id:
                    raise RuntimeError("a different pipeline run became resumable")
            elif state is None:
                raise RuntimeError("the macro pipeline run no longer exists")
            await asyncio.sleep(3)

    async def _start_refresh(self, run_id: int, body: PipelineStartBody) -> int:
        while True:
            status = await self._pipeline.status()
            if status.state not in ("running", "stopping"):
                break
            await self._update_run(run_id, state="waiting_pipeline", current_step="waiting for active pipeline")
            await asyncio.sleep(3)
        stages = [name for name in STAGES if body.stages is None or name in body.stages]
        limits = body.limits.model_dump(exclude_none=True, by_alias=True) if body.limits else {}
        return await self._pipeline.start(
            stages,
            {"limit": body.limit, "limits": limits, "max_pages": body.max_pages, "name": body.name},
        )

    async def _acquire_run_lock(self, run_id: int) -> AsyncConnection | None:
        conn = await self._engine.connect()
        try:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            got = bool(
                (
                    await conn.execute(
                        text("SELECT pg_try_advisory_lock(:namespace, :run_id)"),
                        {"namespace": _MACRO_LOCK_NAMESPACE, "run_id": run_id},
                    )
                ).scalar()
            )
        except BaseException:
            await conn.close()
            raise
        if not got:
            await conn.close()
            return None
        return conn

    async def _execute(self, run_id: int) -> None:
        conn = await self._acquire_run_lock(run_id)
        if conn is None:
            return
        try:
            await self._execute_owned(run_id)
        finally:
            try:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:namespace, :run_id)"),
                    {"namespace": _MACRO_LOCK_NAMESPACE, "run_id": run_id},
                )
            finally:
                await conn.close()

    async def _execute_owned(self, run_id: int) -> None:
        async with self._execution_lock:
            loaded = await self._load(run_id)
            if loaded is None:
                return
            run, macro = loaded
            try:
                await self._update_run(
                    run_id,
                    state="waiting_pipeline" if macro.refresh else "exporting",
                    current_step="refresh" if macro.refresh else "export",
                    started_at=run.started_at or _utcnow(),
                    finished_at=None,
                    error=None,
                )
                if macro.refresh:
                    pipeline_run_id = run.pipeline_run_id
                    if pipeline_run_id is None:
                        pipeline_run_id = await self._start_refresh(
                            run_id, PipelineStartBody.model_validate(macro.pipeline_params)
                        )
                        await self._update_run(
                            run_id,
                            state="running_pipeline",
                            current_step="pipeline",
                            pipeline_run_id=pipeline_run_id,
                        )
                    await self._wait_for_pipeline(run_id, pipeline_run_id)
                await self._update_run(run_id, state="exporting", current_step="search and export")
                outputs = await self._exports.save(
                    SearchBody.model_validate(macro.search_body),
                    [cast(ExportFormat, value) for value in macro.export_formats],
                    title=macro.name,
                    destination=macro.destination,
                )
                await self._update_run(
                    run_id,
                    state="done",
                    current_step=None,
                    outputs=outputs,
                    finished_at=_utcnow(),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._update_run(
                    run_id,
                    state="failed",
                    error=str(exc)[:2_000],
                    finished_at=_utcnow(),
                )
