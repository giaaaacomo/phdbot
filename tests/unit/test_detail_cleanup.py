from phd_searcher.detail_cleanup import (
    DETAIL_CLEANUP_VERSION,
    detail_needs_cleanup,
    detail_refresh_needed,
)


def test_detects_strong_legacy_page_chrome_signals() -> None:
    assert detail_needs_cleanup("Skip to content\nVacancy body")
    assert detail_needs_cleanup("![University logo](https://example.test/logo.svg)\nVacancy body")
    assert detail_needs_cleanup("Vacancy body\nCookie settings")


def test_does_not_flag_normal_job_text_or_missing_details() -> None:
    assert not detail_needs_cleanup(None)
    assert not detail_needs_cleanup("")
    assert not detail_needs_cleanup("We invite applications for this doctoral position.")


def test_refreshes_missing_or_legacy_noisy_details_only_once_per_version() -> None:
    noisy = "Skip to content\nVacancy body"
    assert detail_refresh_needed(None, None)
    assert detail_refresh_needed(noisy, None)
    assert not detail_refresh_needed(noisy, DETAIL_CLEANUP_VERSION)
    assert not detail_refresh_needed("Clean vacancy body", None)
