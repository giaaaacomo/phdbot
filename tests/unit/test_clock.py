"""Calendar cut-offs use the dashboard's Europe/Rome timezone."""

from datetime import UTC, datetime

from phd_searcher.clock import local_today


def test_local_today_crosses_midnight_before_utc() -> None:
    assert local_today(datetime(2026, 8, 20, 23, 30, tzinfo=UTC)).isoformat() == "2026-08-21"


def test_local_today_treats_naive_internal_timestamp_as_utc() -> None:
    assert local_today(datetime(2026, 8, 20, 23, 30)).isoformat() == "2026-08-21"
