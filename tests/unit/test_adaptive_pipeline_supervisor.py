from scripts.adaptive_pipeline_supervisor import adaptive_interval, is_transient_failure


def test_adaptive_interval_spreads_checks_over_long_stage() -> None:
    status = {
        "state": "running",
        "current_stage": {"eta_seconds": 7200, "avg_seconds": 15},
    }

    assert adaptive_interval(status) == 900


def test_adaptive_interval_tightens_near_completion_without_busy_polling() -> None:
    status = {
        "state": "running",
        "current_stage": {"eta_seconds": 90, "avg_seconds": 18},
    }

    assert adaptive_interval(status) == 72


def test_adaptive_interval_observes_deferred_retry_window() -> None:
    status = {
        "state": "running",
        "current_stage": {
            "eta_seconds": 3600,
            "avg_seconds": 10,
            "deferred_queue": {"retry_in_seconds": 40},
        },
    }

    assert adaptive_interval(status) == 45


def test_only_known_operational_failures_are_transient() -> None:
    assert is_transient_failure("scrape retries exhausted for EURAXESS page 177")
    assert is_transient_failure("HTTP 429 rate limit")
    assert not is_transient_failure("StringDataRightTruncationError")
    assert not is_transient_failure(None)
