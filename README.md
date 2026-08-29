# PHDBOT

Backend that collects research and higher-education opportunities from European institutions and
serves semantic search.

FastAPI service with injector DI, litellm model access, and pydantic-settings.

The institution catalog has two explicit tiers: `core` universities obtained from the Wikidata
university hierarchy, and conservatively admitted `specialist` institutions (such as universities
of applied sciences, art/design academies and conservatories). Specialist records must belong to
higher education, have an official website and minimum public documentation, and expose a ROR or
WHED identifier; individually verified institutions can be maintained as curated exceptions.
Audited official vacancy portals can likewise be kept in a small curated-source registry when
generic discovery misses them; they complement normal discovery rather than replacing it.

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
controls), **Coverage** (per-university counts + totals), **Review** (reversible manual and
automatic screening), **Search** (semantic search, detail and portable export), and **Macros**
(saved refresh → search → export workflows). It just calls the JSON endpoints below over the
same origin.

`POST /v1/search {"query": "...", "country": "IT?", "university": "?", "deadline_after": "?"}` → semantic hits.
`GET /v1/positions/{id}`, `POST /v1/positions/{id}/detail-refresh`,
`GET /v1/universities` (coverage), `GET /health` for probes.

The canonical post-scrape cascade is `quality → review → evidence → review2 → enrich`. `quality`
detects malformed URLs, markup/script fragments, navigation and systematically broken extraction
sources without deleting scraped rows; source quarantines are visible in Coverage and are released
for normal triage if a later extraction becomes healthy. `review` is a fast first pass through a
validated LLM tool call (never free-form structured output). Its evidence must be a quote actually
present in the supplied text; automatic approval requires confidence ≥ 0.90 and automatic
rejection ≥ 0.97. Candidates with less than 200 normalized characters of attributable text bypass
the model and go straight to evidence retrieval, so GPU time is not spent judging an empty dossier.

Only the unresolved residue enters `evidence`, which fetches the detail page without changing the
verdict. URL fragments use only their attributable inline excerpt: an HTTP fetch cannot identify a
`#fragment` and must never assign the whole shared page to several positions. `review2` then
independently extracts vacancy/open-status/type facts, validates verbatim evidence and composes the
final status deterministically. It uses the configured local model, one dossier at a time, with a
bounded high-reasoning first pass and short corrective tool passes; there is no cloud judge.
The fallback states remain distinct: a genuinely insufficient source is `source_unusable`, a
structurally valid but unsupported model verdict is quarantined as `grounding_failure`, malformed
tool output stays retryable as `tool_error`, and only evidence-rich residual ambiguity becomes
`human_review`. The rejected model payload is kept in the audit but is never applied. Finally,
`enrich` completes details for eligible items. Every model/manual verdict is
appended to `review_attempts`, while the current verdict stays on the position; manual decisions
always win and survive future refreshes. The audit trail is available at
`GET /v1/positions/{id}/review-attempts`.
The shared position taxonomy includes PhD, Master/MPH, medical doctorate, internships/traineeships,
assistantships, fellowships, postdocs, research staff and faculty roles; every type is available in
the Search filters and portable exports.

Search results can be downloaded as a standalone interactive HTML report, printable PDF, CSV or
JSON. HTML embeds the selected query, filters, result details and client-side filtering, so the
recipient does not need a running PHDBOT instance.

Search has two explicit verification modes. `verified_only` returns final accepted records;
`include_probable` also returns unresolved records that pass hard currentness, source-quality and
non-opportunity exclusions. Probable hits carry explicit heuristic uncertainty tiers (15% grounded,
35% opening status unverified, 60% strong role title with incomplete details); these are audit
levels rather than claimed statistical probabilities. Users can filter and sort by maximum
uncertainty, while the stored review verdict remains unchanged. The GUI can report a bad result to
an append-only audit queue, hide it locally and retract the report, or positively confirm that an
item is a real opportunity. Feedback snapshots the versioned URL family and its extraction context.
Only repeated independent opportunity/non-opportunity labels can produce a visible family prior;
that prior may raise uncertainty but never verifies, rejects or closes an individual sibling. A
single report therefore never creates a global rejection rule, and `closed` is an item-level
availability observation rather than evidence that the family contains fake opportunities.

Each newly collected position records an immutable `first_seen_at` and a scrape-owned
`last_seen_at` (the historical `scraped_at` API field remains as a compatibility alias). Search,
details and portable exports identify when PHDBOT first pulled and last checked the source. Legacy
records keep an unknown first-seen value instead of receiving a fabricated migration date.
Opening a missing or recognizably noisy legacy detail queues an idempotent, attributable refetch;
the existing text remains available until the replacement succeeds. An unlimited detail-enrichment
run also pays down at most 25 high-value legacy captures, so cleanup never turns into an implicit
archive-wide crawl.

The GUI's recommended **Collect & publish** action refreshes universities and sources, scrapes,
applies hard quality rules and indexes position results before enriching the separate institution
index, without waiting for model review. Fast review, evidence retrieval, deep review and detail enrichment remain optional,
bounded precision passes. This keeps time to first useful result independent from the size of the
review pool; that pool is not a mandatory human inbox.

Macros persist a search, pipeline parameters, export formats and a destination below `exports/`.
They can optionally wait for an incremental full refresh before searching and exporting. Macro
runs and pipeline IDs live in PostgreSQL and are recovered after an API restart. A synchronized or
network-shared `exports/` directory can be used as a zero-credential Drive/Nextcloud hand-off.

Pipeline configurations and saved macros can also be scheduled as persistent one-shot jobs from
the GUI. Local input is interpreted explicitly as `Europe/Rome` (including daylight-saving time),
then stored as UTC. `GET/POST /v1/schedules`, `GET /v1/schedules/{id}` and
`POST /v1/schedules/{id}/cancel` expose the same state. Due jobs survive API or host restarts,
wait if another pipeline owns the cluster-wide lock, and are attached idempotently to exactly one
pipeline or macro run. Scheduled pipelines also retry explicit transient failures at most three
times (15-minute cooldown for rate limits, 5 minutes for other known temporary failures); unknown
or persistent errors remain failed and visible for diagnosis.

## Running the pipeline

With the stack up (`make run`; migrations are applied automatically at API startup), run the
scrape stages in the API container (the image bundles Playwright Chromium for the crawl stages):

```bash
make pipeline args="universities"
make pipeline args="discovery --limit 20"
make pipeline args="schema --limit 20"
make pipeline args="scrape"
make pipeline args="quality"
make pipeline args="review"
make pipeline args="evidence"
make pipeline args="review2"
make pipeline args="enrich"
make pipeline args="index"
```

Or drive and monitor it over HTTP without the CLI: `POST /v1/pipeline/start`, `/stop`, `/resume`,
and `GET /v1/pipeline/status` (which stage is running, per-stage average time, ETA). Limits are
independent, for example:

```json
{
  "stages": null,
  "limits": {
    "universities": 50,
    "discovery": 50,
    "schema": 50,
    "scrape": 20,
    "quality": 20,
    "review": 200,
    "evidence": 200,
    "review2": 200,
    "enrich": 200,
    "index": 1000
  },
  "max_pages": 25,
  "name": null
}
```

Run checkpoints and retry/backoff state are persisted in PostgreSQL. `Resume` keeps the same run
and continues from the completed country/university/listing/page/batch instead of restarting the
whole current stage. Successful scrape pages and index batches are committed before advancing the
cursor, so repeating a page after a crash remains safe and idempotent. The status reports active
processing time rather than wall-clock age: stopped/failed intervals are excluded, and a durable
worker heartbeat avoids counting offline time after a crash or power loss.

Schema generation first tries already accepted schemas from the same host and listing kind, but
reuses one only after it passes the normal validation against the current target HTML. A failed
four-step tool-feedback exchange is not replayed wholesale; only transport failures retain the
outer retry budget. This avoids repeated local-model work without trusting a schema blindly.

Limits are per-stage budgets: `scrape` and `quality` count listing sources, `review` and `review2`
count candidate positions, while `evidence` and `enrich` count detail pages. Leaving a limit empty
means all remaining work for that stage. A completed first-pass verdict is not recomputed merely
because a newer run starts; versioned status and append-only attempts make the cascade idempotent.

A local LLM on the host is reachable from containers as `http://host.docker.internal:<port>`
(set `PHD_SEARCHER__LLM__API_BASE` / `PHD_SEARCHER__EMBEDDING__API_BASE`). The `.env`
`localhost` URLs only apply to host-side tooling (`make migrate`, `uv run phd ...`); compose
overrides them for the container.

## Data resilience and bootstrap

Git stores the application, migrations and reproducible configuration—not live
PostgreSQL/Qdrant data. Operational backups may contain source text, contact
details, audit evidence and user configuration, so they belong in encrypted
private storage. A future public fast-start dataset must instead be generated
by an allowlisted, privacy-minimizing export and published as a versioned
release artifact rather than committed to repository history. See
[docs/DATA_BACKUP_AND_BOOTSTRAP.md](docs/DATA_BACKUP_AND_BOOTSTRAP.md).

## Configuration

Env vars, prefix `PHD_SEARCHER__`, nested with `__`. See `.env.example`.

## Layout

- `src/phd_searcher/main.py` — the module-level FastAPI app (`hypercorn phd_searcher.main:app`). Pipeline control (start/stop/resume) is Postgres-mediated, so the server is safe to run with multiple workers (`WEB_CONCURRENCY`).
- `config/` — pydantic-settings. `typedef/` — request/response models + shared types (pure data). `dependency/` — injector modules. `engine/` — `ModelHelper` (litellm) + prompt rendering. `service/` — business logic. `apis/v1/` — routes. `database/` — SQLAlchemy models + Alembic.
- `tests/unit/` — fast, mocked. `tests/integration/` — separate uv project, spins docker compose.
- `Makefile` — dev tasks.
