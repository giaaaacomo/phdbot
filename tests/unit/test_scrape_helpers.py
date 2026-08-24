import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.pipeline import scrape
from phd_searcher.pipeline.normalize import NormalizedPosition
from phd_searcher.pipeline.scrape import (
    _EURAXESS_PAGE_DELAY,
    _EURAXESS_RATE_LIMIT_COOLDOWN,
    _country_code,
    _is_permanent_source_denial,
    _page_budget,
    _position_values,
    _upsert_items,
)


def test_country_code_accepts_alpha_2():
    assert _country_code("it") == "IT"


def test_country_code_maps_euraxess_country_names():
    assert _country_code("Germany") == "DE"
    assert _country_code("United Kingdom") == "GB"


def test_country_code_rejects_unknown_values():
    assert _country_code("Unknown region") is None


def test_network_outage_requires_dns_probe_failure(monkeypatch: pytest.MonkeyPatch):
    async def unavailable():
        return False

    monkeypatch.setattr(scrape, "_internet_dns_available", unavailable)
    error = RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED")
    assert asyncio.run(scrape._network_is_unavailable(error))


def test_single_broken_hostname_is_not_global_outage(monkeypatch: pytest.MonkeyPatch):
    async def available():
        return True

    monkeypatch.setattr(scrape, "_internet_dns_available", available)
    error = RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED")
    assert not asyncio.run(scrape._network_is_unavailable(error))


@pytest.mark.parametrize(
    "message",
    [
        "robots.txt denied access to this resource",
        "Access denied by robots.txt",
        "Access denied by robots.txt: https://example.test/jobs",
        "URL is disallowed by robots.txt",
    ],
)
def test_permanent_source_denial_is_quarantined(message: str):
    assert _is_permanent_source_denial(RuntimeError(message))


def test_transient_fetch_failure_is_not_quarantined():
    assert not _is_permanent_source_denial(RuntimeError("HTTP 503 Service Unavailable"))


@pytest.mark.parametrize(
    "message",
    [
        "request was not denied by robots.txt",
        "not access denied by robots.txt",
        "robots.txt did not block this URL",
        "request did not fail with status code 403",
        "HTTP 403 was not returned",
        "Access denied by robots.txt was not the server response",
    ],
)
def test_negated_source_denial_is_not_quarantined(message: str):
    assert not _is_permanent_source_denial(RuntimeError(message))


def test_empty_page_limit_means_until_empty_with_safety_budget():
    page = ListingPage(url="https://example.test/jobs", pagination_param="page")
    assert _page_budget(page, None) == (1500, True)


def test_explicit_page_limit_is_a_non_exhaustive_test_cap():
    page = ListingPage(url="https://example.test/jobs", pagination_param="page")
    assert _page_budget(page, 25) == (25, False)


def test_non_paginated_source_is_one_exhaustive_page():
    page = ListingPage(url="https://example.test/jobs", pagination_param=None)
    assert _page_budget(page, None) == (1, False)


def test_euraxess_uses_conservative_rate_limits():
    assert _EURAXESS_PAGE_DELAY >= 5
    assert _EURAXESS_RATE_LIMIT_COOLDOWN >= 300


def test_position_observation_time_is_owned_only_by_the_scrape_stage():
    observed_at = datetime(2026, 8, 24, 12, 30)
    page = ListingPage(id=1, url="https://example.test/jobs", kind="university")
    normalized = NormalizedPosition(
        title="PhD in robotics",
        url="https://example.test/jobs/1",
    )

    values = _position_values(normalized, page, observed_at=observed_at)

    assert values["scraped_at"] == observed_at
    assert Position.__table__.c.scraped_at.onupdate is None
    assert Position.__table__.c.first_seen_at.server_default is not None


@pytest.mark.asyncio
async def test_listing_refresh_preserves_a_detail_deadline_when_raw_is_omitted():
    page = ListingPage(
        id=1,
        university_id=7,
        kind="university",
        url="https://example.test/jobs",
    )
    session = SimpleNamespace(execute=AsyncMock())

    assert await _upsert_items(
        session,
        page,
        [{"title": "PhD position in robotics", "url": "/jobs/1"}],
    ) == 1

    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "deadline_raw = coalesce(excluded.deadline_raw, positions.deadline_raw)" in sql
    assert (
        "deadline = CASE WHEN (excluded.deadline_raw IS NOT NULL) "
        "THEN excluded.deadline ELSE positions.deadline END"
    ) in sql
