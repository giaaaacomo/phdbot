from datetime import date

import httpx
import pytest

from phd_searcher.pipeline import source_adapters
from phd_searcher.pipeline.curated_sources import _CURATED_BY_WIKIDATA
from phd_searcher.pipeline.departmental_sources import (
    DTU_MLSM_JOBS_URL,
    STATML_PROJECTS_URL,
    departmental_items,
)
from phd_searcher.pipeline.normalize import normalize_item

STATML = """<h1 class="et_pb_module_header">Specific projects</h1>
<div class="entry-content">
<div class="et_pb_text_inner"><p><strong>Funded PhD Studentship at Imperial in partnership with Example starting 28th September 2026</strong></p></div>
<div class="et_pb_text_inner"><p>A funded opportunity studying robust AI evaluations.</p>
<p>While general admissions are closed, this specific opportunity welcomes applications.</p>
<p><a href="https://statml.io/wp-content/uploads/2026/09/Example.pdf">Project description</a></p>
<p>Expression-of-interest deadline: Friday 11 September 2026, 12:00 noon (UK time).
Interviews start on 14 September.</p></div></div>"""

DTU = """<div class="entry-content"><div class="shapely-content">
<h5> </h5><h5><strong>Open opportunities:</strong></h5>
<h6><strong>PhD Scholarship in Human Navigation</strong></h6>
<h6>DTU offers a 3-year PhD scholarship, in collaboration with Imperial.</h6>
<h6><strong>Application Deadline:</strong> September 15, 2026.
<a href="https://efzu.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001/job/7860">Apply</a></h6>
<hr/><p>No suitable vacancy? Contact us for spontaneous applications.</p>
<h5>Past opportunities:</h5><h6>PhD in old project, July 31, 2025</h6>
<h5>Open opportunities:</h5><h6><strong>Postdoc in archived project</strong></h6>
<h6>Application Deadline: March 10, 2024.</h6></div></div>"""


@pytest.mark.parametrize(
    ("html", "url", "deadline"),
    [(STATML, STATML_PROJECTS_URL, date(2026, 9, 11)), (DTU, DTU_MLSM_JOBS_URL, date(2026, 9, 15))],
)
def test_live_section_only_and_source_deadline_not_start_date(html, url, deadline):
    items = departmental_items(html, url)
    assert len(items) == 1
    position = normalize_item(items[0], base_url=url)
    assert position is not None
    assert position.deadline == deadline
    assert position.position_type == "phd"
    assert "archived project" not in position.description
    assert "spontaneous applications" not in position.description
    if url == STATML_PROJECTS_URL:
        assert "12:00 noon (UK time)" in position.deadline_raw
        assert "general admissions are closed" in position.description
    else:
        assert position.url.endswith("/job/7860")


def test_statml_separates_multiple_projects_without_cross_contamination():
    second = STATML.split('<div class="entry-content">', 1)[1].rsplit("</div>", 1)[0]
    second = second.replace("Example", "Another").replace("11 September", "18 September")
    html = STATML.rsplit("</div>", 1)[0] + second + "</div>"
    items = departmental_items(html, STATML_PROJECTS_URL)
    assert len(items) == 2
    assert items[0]["url"].endswith("Example.pdf")
    assert items[1]["url"].endswith("Another.pdf")
    assert items[0]["__phdbot_deadline_date"] == "2026-09-11"
    assert items[1]["__phdbot_deadline_date"] == "2026-09-18"


@pytest.mark.parametrize(
    ("html", "url"),
    [
        ("Access denied", STATML_PROJECTS_URL),
        ("Access denied", DTU_MLSM_JOBS_URL),
        (STATML.replace("at Imperial", "at Oxford"), STATML_PROJECTS_URL),
        (STATML.replace("deadline:", "interviews:"), STATML_PROJECTS_URL),
        (STATML.replace("statml.io/wp-content", "evil.example/wp-content"), STATML_PROJECTS_URL),
        (DTU.replace("efzu.fa.em2.oraclecloud.com", "evil.example"), DTU_MLSM_JOBS_URL),
        (DTU.replace("September 15, 2026", "not yet known"), DTU_MLSM_JOBS_URL),
        (DTU.replace("<h6>DTU offers", "<h6>PhD Scholarship in another topic; DTU offers"), DTU_MLSM_JOBS_URL),
        (DTU.replace("Open opportunities:", "Changed layout:"), DTU_MLSM_JOBS_URL),
    ],
)
def test_changed_or_ambiguous_source_fails_visibly(html, url):
    with pytest.raises(RuntimeError):
        departmental_items(html, url)


def test_registry_keeps_both_imperial_central_board_and_department():
    urls = [url for url, _ in _CURATED_BY_WIKIDATA["Q189022"]]
    assert STATML_PROJECTS_URL in urls
    assert "https://www.imperial.ac.uk/jobs/search-jobs/" in urls
    assert _CURATED_BY_WIKIDATA["Q1269766"][0][0] == DTU_MLSM_JOBS_URL


@pytest.mark.parametrize(("url", "html"), [(STATML_PROJECTS_URL, STATML), (DTU_MLSM_JOBS_URL, DTU)])
@pytest.mark.parametrize("status", [200, 403, 302])
async def test_fetch_dispatch_and_no_redirect_bypass(monkeypatch, url, html, status):
    original_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(status, text=html)

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        return original_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(source_adapters.httpx, "AsyncClient", client)
    schema = {"adapter": "departmental"}
    if status == 200:
        assert len(await source_adapters.fetch_source_adapter(schema, source_url=url, page_number=0)) == 1
    else:
        with pytest.raises(httpx.HTTPStatusError):
            await source_adapters.fetch_source_adapter(schema, source_url=url, page_number=0)
    assert await source_adapters.fetch_source_adapter(schema, source_url=url, page_number=1) == []
    assert requests == [url]


async def test_untrusted_source_is_not_fetched():
    with pytest.raises(RuntimeError, match="untrusted"):
        await source_adapters.fetch_source_adapter(
            {"adapter": "departmental"}, source_url="https://evil.example/jobs/", page_number=0
        )
