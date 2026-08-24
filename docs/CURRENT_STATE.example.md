# PHDBOT local current state

Copy this file to `docs/CURRENT_STATE.md`. The live file is ignored by Git so
machine-specific run IDs, backup paths and operational details do not become
stale project documentation.

Last updated: YYYY-MM-DD HH:MM timezone.

## Active run

- Run ID and state:
- Current stage/checkpoint:
- Completed and pending stages:
- Safe next operator action:

## Deployed version

- Alembic head:
- Last verification: unit tests, Ruff, mypy:
- Relevant feature/version flags:

## Data and recovery

- PostgreSQL/Qdrant aggregate counts:
- Latest verified local/private backup and checksum:
- Known data gaps or migrations still required:

## Recovery rule

- Leave a running job untouched.
- Resume an explicitly retryable/intentional stop; do not start a replacement.
- Preserve unexpected failures and their recorded error for diagnosis.
