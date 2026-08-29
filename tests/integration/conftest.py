"""Integration fixtures: bring up the full stack via docker compose, hit the real API."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_CMD = ["docker", "compose", "-f", "docker-compose.yaml", "-p", "phd-searcher-test"]
SERVICE_NAME = "api"
LOG_FILE = str(Path(__file__).resolve().parent / "integration.log")
# ponytail: host ports offset by +10 so the test stack coexists with a running dev stack
COMPOSE_ENV = {
    **os.environ,
    "API_HOST_PORT": "8013",
    "POSTGRES_HOST_PORT": "5443",
    "QDRANT_HOST_PORT": "6343",
    "OLLAMA_HOST_PORT": "11534",
}
API_URL = os.environ.get("PHD_SEARCHER_URL", "http://localhost:8013")


def _dump_logs() -> None:
    result = subprocess.run(
        [*COMPOSE_CMD, "logs", SERVICE_NAME, "--tail", "500"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=COMPOSE_ENV,
    )
    Path(LOG_FILE).write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")


@pytest.fixture(scope="session")
def _stack() -> Iterator[None]:
    subprocess.run([*COMPOSE_CMD, "down", "-v", "-t", "0"], cwd=PROJECT_ROOT, capture_output=True, env=COMPOSE_ENV)
    try:
        subprocess.run(
            [*COMPOSE_CMD, "up", "--build", "-d", "--wait"],
            check=True,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            env=COMPOSE_ENV,
        )
        subprocess.run(
            [
                *COMPOSE_CMD,
                "exec",
                "-T",
                SERVICE_NAME,
                "alembic",
                "-c",
                "src/phd_searcher/database/alembic.ini",
                "upgrade",
                "head",
            ],
            check=True,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            env=COMPOSE_ENV,
        )
        yield
    except subprocess.CalledProcessError as e:
        _dump_logs()
        raise RuntimeError(f"docker compose up failed; see {LOG_FILE}") from e
    finally:
        _dump_logs()
        subprocess.run([*COMPOSE_CMD, "down", "-v", "-t", "0"], cwd=PROJECT_ROOT, capture_output=True, env=COMPOSE_ENV)


@pytest.fixture(scope="session")
def client(_stack: None) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=API_URL, timeout=60.0) as c:
        yield c


SEED_SQL = """
INSERT INTO universities (wikidata_id, name, country, website_url, sitelinks, discovery_status)
VALUES ('Q999999', 'Test University', 'IT', 'https://test.example', 0, 'done');
INSERT INTO listing_pages (university_id, url, kind, source, schema_status)
VALUES (
    (SELECT id FROM universities WHERE wikidata_id = 'Q999999'),
    'https://test.example/vacancies', 'university', 'funnel', 'ok'
);
INSERT INTO positions (university_id, listing_page_id, url, title, description, full_description, deadline)
VALUES (
    (SELECT id FROM universities WHERE wikidata_id = 'Q999999'),
    (SELECT id FROM listing_pages WHERE url = 'https://test.example/vacancies'),
    'https://test.example/jobs/1', 'PhD in Testing', 'A test position.',
    'Skip to content ![logo](https://test.example/logo.png) Vacancy details', '2099-01-01'
);
"""


@pytest.fixture(scope="session")
def seeded(_stack: None) -> None:
    subprocess.run(
        [*COMPOSE_CMD, "exec", "-T", "postgres", "psql", "-U", "app", "-d", "app", "-v", "ON_ERROR_STOP=1"],
        input=SEED_SQL,
        check=True,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=COMPOSE_ENV,
    )
