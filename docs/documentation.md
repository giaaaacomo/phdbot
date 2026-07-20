# Phd Searcher — Documentation

## Overview

Backend that scrapes open PhD positions from EU university websites and serves semantic search

## Architecture

FastAPI app (`main.create_app`) with routes in `apis/v1/`. Dependency
injection via `injector` modules (`config` → `ai` → `database` → `service`); handlers
resolve services from the container in `app.state`. Model
access through `ModelHelper` (litellm). Pure data types (request/response models,
aliases) live in `typedef/`. See `docs/superpowers/specs/` for design records.

## Adding an endpoint

1. Add request/response models to `src/phd_searcher/typedef/` (pure `BaseModel`s — no behaviour).
2. Add a `@router.get/post(...)` handler in `src/phd_searcher/apis/v1/routes.py`; inject services via the `Annotated[..., _service(...)]` aliases.
3. Add the service to `service/` and bind it in `dependency/service_module.py`.
4. Cover it in `tests/unit/`.
