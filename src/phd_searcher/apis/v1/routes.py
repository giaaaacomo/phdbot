"""v1 routes. FastAPI handlers; services resolved from the injector container in app.state."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.params import Depends as DependsParam

from phd_searcher.pipeline.runner import STAGES, PipelineError, PipelineRunner
from phd_searcher.service.catalog_service import CatalogService
from phd_searcher.service.search_service import SearchService
from phd_searcher.typedef.pipeline import PipelineStartBody, PipelineStatus
from phd_searcher.typedef.search import CoverageResult, PositionLookup, SearchBody, SearchResult

router = APIRouter(prefix="/v1")


def _service[T](cls: type[T]) -> DependsParam:
    def resolve(request: Request) -> T:
        return cast(T, request.app.state.container.get(cls))  # app.state non è tipizzato

    return DependsParam(resolve)


SearchSvc = Annotated[SearchService, _service(SearchService)]
CatalogSvc = Annotated[CatalogService, _service(CatalogService)]
Runner = Annotated[PipelineRunner, _service(PipelineRunner)]


@router.post("/search")
async def search(body: SearchBody, service: SearchSvc) -> SearchResult:
    return await service.search(body)


@router.get("/positions/{position_id}")
async def position(position_id: int, service: CatalogSvc) -> PositionLookup:
    return await service.position(position_id)


@router.get("/universities")
async def universities(service: CatalogSvc) -> CoverageResult:
    return await service.coverage()


@router.post("/pipeline/start")
async def pipeline_start(body: PipelineStartBody, runner: Runner) -> PipelineStatus:
    stages = [s for s in STAGES if body.stages is None or s in body.stages]  # sempre ordine canonico
    try:
        await runner.start(stages, {"limit": body.limit, "name": body.name})
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
