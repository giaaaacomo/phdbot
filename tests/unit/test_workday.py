import json
from datetime import date

import httpx
import pytest

from phd_searcher.pipeline import workday
from phd_searcher.pipeline.discovery import _hub_links
from phd_searcher.pipeline.normalize import normalize_item
from phd_searcher.pipeline.source_adapters import fetch_source_adapter

URL = "https://example.wd103.myworkdayjobs.com/en-US/Research/?locations=site123"


def test_board_scope_and_live_official_link_required():
    board = workday.workday_board(URL)
    assert board is not None
    assert board.facets == {"locations": ["site123"]}
    assert workday.recruitment_referrer("https://lab.example/careers/", "https://lab.example/")
    assert not workday.recruitment_referrer("https://other.example/careers/", "https://lab.example/")
    assert not workday.recruitment_referrer("https://lab.example/news/", "https://lab.example/")
    assert workday.linked_workday_board(
        f'<a href="{URL}">See all institute jobs</a>', "https://lab.example/careers/", URL
    )
    assert not workday.linked_workday_board(
        f'<a href="{URL}">Jobs</a>', "https://lab.example/careers/", URL.replace("site123", "other")
    )
    assert not workday.linked_workday_board(
        f'<a href="{URL}">Funding partners</a>', "https://lab.example/careers/", URL
    )


@pytest.mark.parametrize(
    "url",
    [
        URL.replace(".com/", ".com.evil.example/"),
        URL.replace("https:", "http:"),
        URL + "&unsupportedFilter=abc",
        URL + "&locations=",
        URL.replace("example.wd103", "user:secret@example.wd103"),
        URL.replace("/Research/", "/Research/job/abc"),
    ],
)
def test_unsupported_scope_or_host_never_silently_broadens(url):
    assert workday.workday_board(url) is None


def test_downloads_are_not_html_discovery_hubs():
    assert _hub_links(
        [
            {"href": "https://example.org/phd-brochure.pdf", "text": "Careers"},
            {"href": "https://example.org/careers", "text": "Careers"},
        ]
    ) == ["https://example.org/careers"]


async def test_adapter_preserves_facets_pagination_and_detail_dates(monkeypatch):
    original = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "POST":
            data = json.loads(request.content)
            assert data["appliedFacets"] == {"locations": ["site123"]}
            assert data["limit"] == 20
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "jobPostings": [
                        {"externalPath": "/job/Site/PhD_JR1", "title": "PhD"},
                    ]
                    if data["offset"] in {0, 20}
                    else [],
                },
            )
        return httpx.Response(
            200,
            json={
                "jobPostingInfo": {
                    "title": "PhD in simulation",
                    "jobDescription": "<p>Research project.</p><p>Apply here.</p>",
                    "startDate": "2026-09-01",
                    "endDate": "2026-10-01",
                }
            },
        )

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(workday.httpx, "AsyncClient", client)
    items = await fetch_source_adapter({"adapter": "workday"}, source_url=URL, page_number=0)
    assert items is not None
    assert len(items) == 1
    item = normalize_item(items[0], base_url=URL)
    assert item is not None
    assert item.deadline == date(2026, 10, 1)
    assert item.published_at == date(2026, 9, 1)
    assert item.description == "Research project.\nApply here."
    assert item.url == "https://example.wd103.myworkdayjobs.com/en-US/Research/job/Site/PhD_JR1"
    assert await fetch_source_adapter({"adapter": "workday"}, source_url=URL, page_number=1) == []
    assert len(requests) == 3


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"total": 2, "jobPostings": []},
        {"total": 0, "jobPostings": [{"externalPath": "/job/a"}]},
        {"total": 1, "jobPostings": [{"externalPath": "https://evil.example/job/a"}]},
    ],
)
async def test_changed_or_inconsistent_feed_is_failure_not_empty(monkeypatch, payload):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        workday.httpx,
        "AsyncClient",
        lambda **kw: original(**kw, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))),
    )
    with pytest.raises(RuntimeError, match="Workday"):
        await workday.fetch_workday_page(URL, 0)
