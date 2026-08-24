# PHDBOT operations runbook

## Services

- Dashboard/API: <http://localhost:8003>
- Ollama: <http://localhost:11434>
- Start or rebuild the stack: `make run`
- Inspect it: `docker compose ps --all`
- Stop it deliberately: `make stop`
- Update Ollama explicitly when desired: `docker compose pull ollama`

The lock screen is harmless, but system suspension, loss of power, or loss of
network can interrupt active work. Pipeline progress and retry cursors are
stored in PostgreSQL.

## Automatic ultrawide refresh

`make display-refresh-install` installs and enables a host-side user service.
It checks PHDBOT every five minutes, so applying or restoring the reduced mode
can lag a pipeline transition by at most roughly five minutes.
While the pipeline state is `running` or `stopping`, it temporarily changes the
sole external display from its current native mode to the lowest safe native
fixed-refresh mode near the 60 Hz target. If the monitor does not advertise a
native 60 Hz mode, the nearest lower native mode is used; the helper never
lowers the resolution or forces a custom mode. With multiple external displays,
pass explicit `--connector` and optionally `--product` arguments in the local
service. It restores the exact previous mode after `done`, `stopped` or
`failed`, and after the API has remained unavailable for 60 seconds.

The helper preserves the complete live GNOME layout, verifies the new
configuration before applying it, never writes the persistent GNOME monitor
profile, and does not override a display mode changed manually during a run.
Display failures are logged but cannot fail or stop PHDBOT. Inspect it with
`make display-refresh-status` or `journalctl --user -u phdbot-display-refresh`.

## Pipeline controls

- **Start new run** creates a new run. Do not use it to recover an interrupted
  one.
- **Resume interrupted run** continues its durable checkpoints and retry
  budgets.
- **Stop current run** is a checkpoint-preserving stop, not a pause of the
  current process.
- **Collect & publish** is the recommended autonomous path: it runs collection,
  hard quality checks, institution enrichment and indexing, but deliberately
  defers model review/evidence/detail refinement. It produces a useful
  verified-plus-labelled-probable catalogue instead of waiting for the review
  backlog to become empty.
- No stages selected means the canonical full pipeline. Selecting stages runs
  only those stages, still in canonical order.
- Empty stage limit means all candidates for that stage.

## Review cascade

The intended inexpensive-to-expensive order is quality gate, fast review,
evidence collection, targeted deep review, eligible-detail enrichment, and
search indexing. Deep review must be restricted to live, evidence-ready,
genuinely ambiguous records; unreachable and evidence-poor sources should be
retried or deferred without consuming LLM time.

This cascade is a precision refinement, not a publication barrier. A large
`review` count is neither a mandatory GPU workload nor a human inbox. Publish
safe provisional leads first, with visible uncertainty, and give expensive
review stages explicit budgets chosen for expected information gain. The
catalogue remains useful while refinement continues in later runs.

Searchability and verification are separate concepts. Verified records may be
shown normally. Provisional records, when enabled, must carry their status in
the index and UI, remain filterable, and be removable through feedback.

The Search tab defaults to **verified + probable**. `verified_only` is the
strict view. Probable records retain all hard exclusions (expired/closed,
known noise, broken extraction and unhealthy sources) but use transparent
heuristic uncertainty tiers: 15% for a grounded/current lead, 35% when the
application is recognizable but its current opening is not proven, and 60%
for a strong role-shaped title with incomplete detail evidence. These are
audit tiers, not calibrated probabilities. **Maximum uncertainty** lets users
choose their own recall/precision tradeoff; badges expose the exact missing
facts. Search metadata never silently promotes the PostgreSQL verdict.

Reporting a result writes an append-only feedback record and hides the item in
that browser. Undo retracts the report. A single report never creates a global
domain rule or deletes a position; exports and other users remain unaffected
until a future audited feedback policy is explicitly implemented.

## Persistent schedules

- Schedule times entered by the GUI are interpreted in `Europe/Rome` and
  persisted in UTC.
- A scheduled pipeline waits for the cluster-wide run lock and attaches to at
  most one pipeline run, including after an API/host restart.
- Inspect active jobs with `GET /v1/schedules?active_only=true` and one job with
  `GET /v1/schedules/{id}`. Cancel only before it starts with
  `POST /v1/schedules/{id}/cancel`.
- A scheduled pipeline automatically resumes only explicit transient failures
  (for example 429, timeouts, connection resets or temporary Ollama outages),
  at most three times after the initial attempt. Rate limits cool down for 15
  minutes; other transient errors for 5 minutes. A genuine or exhausted
  failure remains recoverable through Pipeline Resume and is never looped.

`make completion-install schedule=N` optionally installs a token-free user
service for one persistent scheduled job. It checks every five minutes and
never interrupts an active pipeline. At a normal completion it snapshots the
catalogue, deploys the already-built API image, enriches at most 700 eligible
detail pages and then launches the final index reconciliation. The cap keeps
the unattended network workload bounded while using the morning window for
candidate-ready data. If the scheduled run fails terminally, the same bounded
follow-up salvages whatever verified/probable results are already usable; a
deliberate Stop is respected and produces only a report. Inspect it with
`make completion-status schedule=N` or under
`~/.local/state/phdbot/completion-N.json`. This service cannot wake or invoke a
Codex conversation; it performs only predefined local actions without tokens.
The installer renders the service with the current checkout path; override it
with `make completion-install schedule=N PROJECT_DIR=/absolute/path` when needed.

## Recovery checklist

1. Inspect `/v1/pipeline/status` or the Pipeline tab.
2. For an explicit retry exhaustion message, use Resume.
3. For another error, preserve the run and inspect its recorded error/logs.
4. Verify PostgreSQL, Qdrant, Ollama, and API health before any new Start.
5. After a restored database or missing Qdrant collection, run index sync so
   PostgreSQL remains authoritative.

## Verification before an unattended run

1. Backup and restore metadata verified when data/schema changes are involved.
2. Unit tests, Ruff, and mypy green for the deployed code.
3. Targeted production canaries audited.
4. Disk, GPU, and all service health checks pass.
5. Schedule is stored, active, and rendered in `Europe/Rome` at the intended
   local time.
6. Workload is bounded to finish with a safety margin; otherwise Stop and
   Resume at the next low-cost window.
