"""Service entry. Exposes a module-level FastAPI app (hypercorn phd_searcher.main:app)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from injector import Injector
from starlette.requests import Request
from starlette.responses import Response

from phd_searcher import __version__
from phd_searcher.apis.v1 import routes
from phd_searcher.dependency import container
from phd_searcher.service.macro_service import MacroService
from phd_searcher.service.schedule_service import ScheduleService


def create_app(
    container: Injector,
    *,
    title: str = "PHDBOT",
    version: str = __version__,
    recover_durable_macros: bool = False,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        macro_service: MacroService | None = None
        schedule_service: ScheduleService | None = None
        if recover_durable_macros:
            macro_service = container.get(MacroService)
            schedule_service = container.get(ScheduleService)
            await macro_service.recover()
            await schedule_service.start()
        try:
            yield
        finally:
            if schedule_service is not None:
                await schedule_service.shutdown()
            if macro_service is not None:
                await macro_service.shutdown()

    app = FastAPI(title=title, version=version, lifespan=lifespan)
    app.state.container = container
    app.include_router(routes.router)

    @app.middleware("http")
    async def prevent_stale_dashboard(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Control-panel GUI, served last so /v1, /health, /docs win. Same-origin → no CORS. Local-only.
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")

    return app


app = create_app(container, recover_durable_macros=True)
