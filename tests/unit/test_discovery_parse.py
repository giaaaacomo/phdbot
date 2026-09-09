"""Tests for discovery reply parsing."""

from __future__ import annotations

from phd_searcher.pipeline.discovery import _candidates, _Link, _merge_candidate_groups, _parse_reply, _same_site

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


def test_hub_candidates_survive_a_saturated_homepage_budget():
    homepage = [_Link(f"https://uni.example/phd/info-{i}", str(i)) for i in range(30)]
    sitemap = [_Link(f"https://uni.example/jobs/{i}", str(i)) for i in range(30)]
    department = _Link("https://uni.example/medicine/studentships", "Funded projects")
    merged = _merge_candidate_groups([homepage, sitemap, [department]])
    assert len(merged) == 30
    assert merged[:3] == [homepage[0], sitemap[0], department]
    assert len({candidate.href for candidate in merged}) == 30


def test_duplicate_hubs_do_not_consume_the_candidate_budget():
    shared = _Link("https://uni.example/phd", "PhD opportunities")
    unique = _Link("https://uni.example/medicine/studentships", "Funded projects")
    assert _merge_candidate_groups([[shared], [shared, unique], [], [shared]]) == [shared, unique]
    assert _merge_candidate_groups([]) == []


def test_studentships_are_candidates_even_without_phd_in_the_link():
    links = [{"href": "https://uni.example/medicine/studentships", "text": "Funded projects"}]
    assert len(_candidates(links)) == 1
