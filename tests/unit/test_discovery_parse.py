"""Tests for discovery reply parsing."""

from __future__ import annotations

from phd_searcher.pipeline.discovery import _candidates, _parse_reply, _same_site

ALLOWED = {"https://a.example/phd", "https://b.example/vacancies"}


def test_parse_reply_filters_hallucinations():
    reply = '["https://a.example/phd", "https://evil.example/x"]'
    assert _parse_reply(reply, ALLOWED) == ["https://a.example/phd"]


def test_parse_reply_handles_fenced_json():
    reply = '```json\n["https://b.example/vacancies"]\n```'
    assert _parse_reply(reply, ALLOWED) == ["https://b.example/vacancies"]


def test_parse_reply_garbage_is_empty():
    assert _parse_reply("NONE", ALLOWED) == []


def test_parse_reply_non_list_json_is_empty():
    assert _parse_reply('{"a": 1}', ALLOWED) == []


def test_parse_reply_empty_array():
    assert _parse_reply("[]", ALLOWED) == []


def test_candidates_exclude_downloadable_documents():
    links = [
        {"href": "https://uni.example/phd/vacancies", "text": "PhD vacancies"},
        {"href": "https://uni.example/phd/call.docx", "text": "PhD call"},
        {"href": "https://uni.example/phd/rules.pdf?download=1", "text": "Doctoral positions"},
    ]
    assert [candidate.href for candidate in _candidates(links)] == ["https://uni.example/phd/vacancies"]


def test_same_site_accepts_official_domain_and_subdomains():
    website = "https://www.example.ac.uk/school"
    assert _same_site("https://example.ac.uk/jobs", website)
    assert _same_site("https://careers.example.ac.uk/openings", website)


def test_same_site_rejects_other_universities_and_aggregators():
    website = "https://www.hesge.ch/head/en"
    assert not _same_site("https://jobs.ethz.ch/site/setlang/en", website)
    assert not _same_site("https://academicpositions.com/jobs/position/phd", website)
