#!/bin/bash
set -euo pipefail

# Con argomenti: esegui quelli (pipeline, alembic, ...). Senza: lancia il server.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Il controllo pipeline è mediato da Postgres (advisory lock), quindi WEB_CONCURRENCY può salire.
exec hypercorn phd_searcher.main:app --bind 0.0.0.0:8000 --workers "${WEB_CONCURRENCY:-1}"
