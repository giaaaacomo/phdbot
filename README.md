# Phd Searcher

Backend that scrapes open PhD positions from EU university websites and serves semantic search

FastAPI service with injector DI, litellm model access, and pydantic-settings.

## Quickstart

```bash
make setup          # uv sync (main + integration), enable git hooks
make run            # bring up API + postgres + qdrant via docker compose (API on :8003)
make migrate        # apply DB migrations
make test-unit      # unit tests
make stop           # tear the stack down
```

## Control panel (GUI)

With the stack up (`make run`), open <http://localhost:8003/> — a single-page local admin UI
served by the API itself (no extra container). Tabs: **Pipeline** (live status + start/stop/resume
controls), **Coverage** (per-university counts + totals), **Search** (semantic search + position
detail). It just calls the JSON endpoints below over the same origin.

`POST /v1/search {"query": "...", "country": "IT?", "university": "?", "deadline_after": "?"}` → semantic hits.
`GET /v1/positions/{id}`, `GET /v1/universities` (coverage), `GET /health` for probes.

## Running the pipeline

With the stack up (`make run`) and migrated (`make migrate`), run the scrape stages in the
API container (the image bundles Playwright Chromium for the crawl stages):

```bash
make pipeline args="universities"
make pipeline args="discovery --limit 20"
make pipeline args="schema --limit 20"
make pipeline args="scrape"
make pipeline args="index"
```

Or drive and monitor it over HTTP without the CLI: `POST /v1/pipeline/start` (optional
`{"stages": [...], "limit": N, "name": "..."}`), `/stop`, `/resume`, and
`GET /v1/pipeline/status` (which stage is running, per-stage average time, ETA).

A local LLM on the host is reachable from containers as `http://host.docker.internal:<port>`
(set `PHD_SEARCHER__LLM__API_BASE` / `PHD_SEARCHER__EMBEDDING__API_BASE`). The `.env`
`localhost` URLs only apply to host-side tooling (`make migrate`, `uv run phd ...`); compose
overrides them for the container.

## Configuration

Env vars, prefix `PHD_SEARCHER__`, nested with `__`. See `.env.example`.

## Layout

- `src/phd_searcher/main.py` — the module-level FastAPI app (`hypercorn phd_searcher.main:app`). Pipeline control (start/stop/resume) is Postgres-mediated, so the server is safe to run with multiple workers (`WEB_CONCURRENCY`).
- `config/` — pydantic-settings. `typedef/` — request/response models + shared types (pure data). `dependency/` — injector modules. `engine/` — `ModelHelper` (litellm) + prompt rendering. `service/` — business logic. `apis/v1/` — routes. `database/` — SQLAlchemy models + Alembic.
- `tests/unit/` — fast, mocked. `tests/integration/` — separate uv project, spins docker compose.
- `Makefile` — dev tasks.
