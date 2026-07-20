FROM python:3.13-slim AS base

RUN groupadd -g 1000 appuser && useradd -u 1000 -g 1000 -m appuser
RUN mkdir -p /app && chown appuser:appuser /app
WORKDIR /app

COPY --chown=appuser:appuser apt-packages.txt ./
RUN if [ -s apt-packages.txt ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends $(cat apt-packages.txt) \
        && rm -rf /var/lib/apt/lists/*; \
    fi
RUN pip install --no-cache-dir uv

FROM base AS builder
COPY --chown=appuser:appuser pyproject.toml uv.lock README.md /app/
USER appuser
ENV UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
RUN --mount=type=cache,target=/home/appuser/.cache/uv,uid=1000 \
    uv sync --frozen --no-install-project --no-dev --no-editable

FROM base AS runtime
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# Chromium + system deps per crawl4ai (stadi discovery/schema/scrape); path condiviso, install da root
RUN /app/.venv/bin/playwright install --with-deps chromium
COPY --chown=appuser:appuser src /app/src
COPY --chown=appuser:appuser docker-entrypoint.sh /app/docker-entrypoint.sh
COPY --chown=appuser:appuser healthcheck.py /app/healthcheck.py
RUN chmod +x /app/docker-entrypoint.sh
USER appuser
EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
