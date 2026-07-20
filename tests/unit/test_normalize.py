from datetime import date

from phd_searcher.pipeline.normalize import normalize_item, parse_deadline


def test_parse_deadline_iso_embedded():
    assert parse_deadline("Deadline: 2026-09-01 23:59") == date(2026, 9, 1)


def test_parse_deadline_european_format():
    assert parse_deadline("01/09/2026") == date(2026, 9, 1)


def test_parse_deadline_unparseable_is_none():
    assert parse_deadline("rolling admission") is None
    assert parse_deadline(None) is None


def test_normalize_joins_relative_url():
    item = {"title": "PhD in ML", "url": "/jobs/123", "deadline": "2026-10-01"}
    n = normalize_item(item, base_url="https://uni.example/vacancies")
    assert n is not None
    assert n.url == "https://uni.example/jobs/123"
    assert n.deadline == date(2026, 10, 1)


def test_normalize_drops_items_without_title():
    assert normalize_item({"url": "/x"}, base_url="https://uni.example/") is None


def test_normalize_drops_items_without_href():
    assert normalize_item({"title": "PhD"}, base_url="https://uni.example/") is None


def test_normalize_truncates_long_deadline_raw():
    n = normalize_item({"title": "PhD", "url": "/x", "deadline": "x" * 500}, base_url="https://uni.example/")
    assert n is not None
    assert n.deadline_raw is not None
    assert len(n.deadline_raw) == 256
