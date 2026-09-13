import json

import pytest

from phd_searcher.pipeline.quality_gate import QualityDisposition, inspect_candidate
from phd_searcher.pipeline.source_validation import employer_evidence, page_state


@pytest.mark.parametrize("status", [404, 410])
def test_http_error_is_not_an_empty_listing(status):
    assert page_state("", status) == "unavailable"


@pytest.mark.parametrize("heading", ["Not Found", "Page not found", "404 - Page Not Found"])
def test_soft_404_heading(heading):
    assert page_state(f"<main><h1>{heading}</h1></main>", 200) == "unavailable"


def test_ofai_no_openings_is_empty_not_a_funding_catalog():
    html = '<main><h1>Jobs</h1><p>At the moment there are no job openings. Please check back later.</p><p>We welcome enquiries from funded researchers through <a href="https://funding.example/">MSCA fellowships</a>.</p></main>'
    assert page_state(html, 200) == "empty"


@pytest.mark.parametrize(
    "body",
    [
        "<p>There are no job openings in administration.</p><p>Research positions are open.</p>",
        '<p>There are no job openings.</p><a href="/job/1">PhD in Machine Learning</a>',
        '<p>There are no job openings.</p><script type="application/ld+json">{"@type":"JobPosting"}</script>',
        "<h1>PhD in missing data</h1><p>The result was not found.</p>",
    ],
)
def test_mixed_or_unrelated_negative_text_does_not_hide_jobs(body):
    assert page_state(f"<main>{body}</main>", 200) is None


def test_external_ats_requires_employer_specific_jobposting_not_mention():
    assert not employer_evidence("<h1>EU programmes</h1><a>Example Institute</a>", "Example Institute")
    payload = {
        "@graph": [
            {"@type": "JobPosting", "hiringOrganization": {"@type": "Organization", "name": "Example Institute"}}
        ]
    }
    html = f'<script type="application/ld+json">{json.dumps(payload)}</script>'
    assert employer_evidence(html, "Example Institute")
    assert not employer_evidence(html, "Other Institute")
    assert not employer_evidence(html, "")
    payload["@graph"].append({"@type": "JobPosting", "hiringOrganization": {"name": "Another University"}})
    assert not employer_evidence(
        f'<script type="application/ld+json">{json.dumps(payload)}</script>', "Example Institute"
    )
    assert not employer_evidence(
        '<script type="application/ld+json">{"@type":"Organization","name":"Example Institute"}</script>',
        "Example Institute",
    )


@pytest.mark.parametrize("title", ["Not Found", "404", "Page not found"])
def test_old_error_extractions_are_quarantined_not_searchable(title):
    assert inspect_candidate(title=title, url="https://example.org/jobs").disposition == QualityDisposition.QUARANTINE
