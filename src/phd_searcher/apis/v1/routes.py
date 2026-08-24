"""v1 routes. FastAPI handlers; services resolved from the injector container in app.state."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.params import Depends as DependsParam

from phd_searcher.pipeline.runner import STAGES, PipelineError, PipelineRunner
from phd_searcher.service.catalog_service import CatalogService
from phd_searcher.service.export_service import ExportService
from phd_searcher.service.feedback_service import FeedbackService
from phd_searcher.service.macro_service import MacroService
from phd_searcher.service.schedule_service import ScheduleService
from phd_searcher.service.search_service import SearchService
from phd_searcher.typedef.feedback import PositionFeedbackCreate, PositionFeedbackView
from phd_searcher.typedef.macro import MacroCreate, MacroRunView, MacroView
from phd_searcher.typedef.pipeline import PipelineStartBody, PipelineStatus
from phd_searcher.typedef.schedule import ScheduleCreate, ScheduleView
from phd_searcher.typedef.search import (
    CoverageResult,
    ExportBody,
    PositionLookup,
    ReviewAttemptItem,
    ScreeningItem,
    ScreeningPage,
    ScreeningStatus,
    ScreeningUpdate,
    SearchBody,
    SearchFacets,
    SearchResult,
)

router = APIRouter(prefix="/v1")


def _service[T](cls: type[T]) -> DependsParam:
    def resolve(request: Request) -> T:
        return cast(T, request.app.state.container.get(cls))  # app.state non è tipizzato

    return DependsParam(resolve)


SearchSvc = Annotated[SearchService, _service(SearchService)]
CatalogSvc = Annotated[CatalogService, _service(CatalogService)]
Runner = Annotated[PipelineRunner, _service(PipelineRunner)]
ExportSvc = Annotated[ExportService, _service(ExportService)]
FeedbackSvc = Annotated[FeedbackService, _service(FeedbackService)]
MacroSvc = Annotated[MacroService, _service(MacroService)]
ScheduleSvc = Annotated[ScheduleService, _service(ScheduleService)]


@router.post("/search")
async def search(body: SearchBody, service: SearchSvc) -> SearchResult:
    return await service.search(body)


@router.post("/exports")
async def export_results(body: ExportBody, service: ExportSvc) -> Response:
    content, content_type, filename = await service.render(body)
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/macros")
async def macros(service: MacroSvc) -> list[MacroView]:
    return await service.list()


@router.post("/macros")
async def create_macro(body: MacroCreate, service: MacroSvc) -> MacroView:
    try:
        return await service.create(body)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/macros/{macro_id}/run")
async def run_macro(macro_id: int, service: MacroSvc) -> MacroRunView:
    try:
        run = await service.run(macro_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="macro not found")
    return run


@router.get("/macro-runs/{run_id}")
async def macro_run(run_id: int, service: MacroSvc) -> MacroRunView:
    run = await service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="macro run not found")
    return run


@router.get("/schedules")
async def schedules(
    service: ScheduleSvc,
    active_only: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ScheduleView]:
    return await service.list(active_only=active_only, limit=limit)


@router.post("/schedules")
async def create_schedule(body: ScheduleCreate, service: ScheduleSvc) -> ScheduleView:
    try:
        return await service.create(body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/schedules/{job_id}")
async def schedule(job_id: int, service: ScheduleSvc) -> ScheduleView:
    job = await service.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return job


@router.post("/schedules/{job_id}/cancel")
async def cancel_schedule(job_id: int, service: ScheduleSvc) -> ScheduleView:
    try:
        job = await service.cancel(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return job


@router.get("/positions/{position_id}")
async def position(position_id: int, service: CatalogSvc) -> PositionLookup:
    return await service.position(position_id)


@router.post("/positions/{position_id}/feedback", status_code=201)
async def create_position_feedback(
    position_id: int,
    body: PositionFeedbackCreate,
    service: FeedbackSvc,
) -> PositionFeedbackView:
    feedback = await service.create(position_id, body)
    if feedback is None:
        raise HTTPException(status_code=404, detail="position not found")
    return feedback


@router.post("/positions/{position_id}/feedback/{feedback_id}/retract")
async def retract_position_feedback(
    position_id: int,
    feedback_id: int,
    service: FeedbackSvc,
) -> PositionFeedbackView:
    feedback = await service.retract(position_id, feedback_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="feedback not found")
    return feedback


@router.get("/positions/{position_id}/review-attempts")
async def position_review_attempts(position_id: int, service: CatalogSvc) -> list[ReviewAttemptItem]:
    return await service.review_attempts(position_id)


@router.post("/positions/{position_id}/screening")
async def update_screening(
    position_id: int,
    body: ScreeningUpdate,
    service: CatalogSvc,
) -> ScreeningItem:
    item = await service.update_screening(position_id, body)
    if item is None:
        raise HTTPException(status_code=404, detail="position not found")
    return item


@router.get("/screening")
async def screening(
    service: CatalogSvc,
    status: ScreeningStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ScreeningPage:
    return await service.screening(status, limit=limit, offset=offset)


@router.get("/universities")
async def universities(service: CatalogSvc) -> CoverageResult:
    return await service.coverage()


@router.get("/search/facets")
async def search_facets(service: CatalogSvc) -> SearchFacets:
    return await service.search_facets()


@router.post("/pipeline/start")
async def pipeline_start(body: PipelineStartBody, runner: Runner) -> PipelineStatus:
    stages = [s for s in STAGES if body.stages is None or s in body.stages]  # sempre ordine canonico
    limits = body.limits.model_dump(exclude_none=True, by_alias=True) if body.limits is not None else {}
    try:
        await runner.start(
            stages,
            {"limit": body.limit, "limits": limits, "max_pages": body.max_pages, "name": body.name},
        )
    except PipelineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await runner.status()


@router.post("/pipeline/stop")
async def pipeline_stop(runner: Runner) -> PipelineStatus:
    try:
        await runner.stop()
    except PipelineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await runner.status()


@router.post("/pipeline/resume")
async def pipeline_resume(runner: Runner) -> PipelineStatus:
    try:
        await runner.resume()
    except PipelineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await runner.status()


@router.get("/pipeline/status")
async def pipeline_status(runner: Runner) -> PipelineStatus:
    return await runner.status()
