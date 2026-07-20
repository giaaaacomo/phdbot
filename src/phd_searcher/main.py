"""Service entry. Exposes a module-level FastAPI app (hypercorn phd_searcher.main:app)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from injector import Injector

from phd_searcher import __version__
from phd_searcher.apis.v1 import routes
from phd_searcher.dependency import container


def create_app(container: Injector, *, title: str = "Phd Searcher", version: str = __version__) -> FastAPI:
    app = FastAPI(title=title, version=version)
    app.state.container = container
    app.include_router(routes.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Control-panel GUI, served last so /v1, /health, /docs win. Same-origin → no CORS. Local-only.
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")

    return app


app = create_app(container)
