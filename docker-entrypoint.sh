#!/bin/bash
set -euo pipefail

# Con argomenti: esegui quelli (pipeline, alembic, ...). Senza: lancia il server.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Upgrade idempotente prima di accettare richieste: necessario perché i checkpoint
# devono esistere anche dopo un semplice `make run` su un database già popolato.
alembic -c /app/src/phd_searcher/database/alembic.ini upgrade head

# Il controllo pipeline è mediato da Postgres (advisory lock), quindi WEB_CONCURRENCY può salire.
exec hypercorn phd_searcher.main:app --bind 0.0.0.0:8000 --workers "${WEB_CONCURRENCY:-1}"
