def test_health_ok(client):
    assert client.get("/health").status_code == 200


def test_dashboard_is_not_cached(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "<title>PHDBOT — control panel</title>" in response.text
    assert "<h1>PHDBOT</h1>" in response.text
    assert "PhD Searcher" not in response.text
    assert "pages/source" in response.text
    assert "duration" in response.text
    assert "Maximum results" in response.text
    assert "Minimum compensation" in response.text
    assert "Minimum relevance" in response.text
    assert "Detail pages" in response.text
    assert "Related universities" in response.text
    assert "Candidate screening" in response.text
    assert 'review2: ["Deep review"' in response.text
    assert "const stageLabel = stage" in response.text
    assert 'title="Continue the interrupted run from its durable checkpoint"' in response.text
    assert 'title="Exclude results below this semantic similarity threshold"' in response.text
    assert "review-filter-tile" in response.text
    assert "data-review-status" in response.text
    assert 'aria-pressed="${status === value}"' in response.text
    assert "country-mark" in response.text
    assert "countryMark(u.country)" in response.text
    assert 'title="${esc(countryName(normalized))}"' in response.text
    assert 'data-cov-sort="tier"' in response.text
    assert 'data-cov-sort="discovery"' in response.text
    assert 'data-cov-sort="pages"' in response.text
    assert "updateCoverageSortHeaders" in response.text
    assert "max-width: 3440px" in response.text
    assert "#pipeline.active { display: grid" in response.text
    assert "#search .filter-layout { grid-template-columns" in response.text
    assert "@media (min-width: 1700px)" in response.text
    assert "#cov-tiles { grid-template-columns: repeat(6" in response.text
    assert "#review-tiles { grid-template-columns: repeat(5" in response.text
    assert "@media (min-width: 2200px)" in response.text
    assert "max-width: 100ch; margin-inline: auto" in response.text
    assert "grid-template-columns: repeat(2, minmax(0, 100ch))" in response.text
    assert "numbered-hit" in response.text
    assert "page.offset + index + 1" in response.text
    assert '<th class="row-number"' in response.text
    assert "Searching indexed opportunities" in response.text
    assert 'aria-busy", String(active)' in response.text
    assert "finally { setSearchLoading(false); }" in response.text
    assert 'RESULT_VIEWS_KEY = "phdbot.resultViews.v1"' in response.text
    assert 'id="s-mode"' in response.text
    assert 'value="include_probable"' in response.text
    assert 'id="s-max-uncertainty" type="number" min="0" max="100" step="5" value="60"' in response.text
    assert "Verification evidence unavailable" in response.text
    assert 'REPORTED_POSITIONS_KEY = "phdbot.reportedPositions.v1"' in response.text
    assert "submitPositionFeedback" in response.text
    assert "undoPositionFeedback" in response.text
    assert ".verification-badge.probable" in response.text
    assert "markResultViewed(id, \"details\")" in response.text
    assert "Source opened" in response.text
    assert 'value === "phd"' in response.text
    assert "PhD / doctoral / predoctoral position" in response.text
    assert 'role="tablist"' in response.text
    assert 'TAB_KEY = "phdbot.activeTab.v1"' in response.text
    assert "Loading candidates" in response.text
    assert "Filters reset; semantic query preserved" in response.text
    assert "coverageLoadToken" in response.text
    assert "reviewLoadToken" in response.text
    assert "Stop the current run safely" in response.text
    assert 'data-tab="review"' in response.text
    assert 'data-tab="macros"' in response.text
    assert "Refine automatic sample" in response.text
    assert 'id="btn-export"' in response.text
    assert "Save current search as macro" in response.text
    assert 'id="p-schedule-at" type="datetime-local"' in response.text
    assert 'id="btn-schedule"' in response.text
    assert 'target:"pipeline", run_at:runAt, timezone:"Europe/Rome"' in response.text
    assert "pipelineBodyFromControls" in response.text
    assert "scheduleMacro" in response.text
    assert "/v1/schedules?limit=100" in response.text
    assert 'class="act ghost" id="btn-stop" disabled' in response.text
    assert 'classList.toggle("danger", canStop)' in response.text
    assert 'classList.toggle("ghost", resumable)' in response.text
    assert "Dates and compensation" in response.text
    assert "Relevance and result order" in response.text
    assert "institutions" in response.text
    assert "Collect &amp; publish" in response.text
    assert "until the first empty page" in response.text
    assert 'id="p-max-pages" type="number" min="1" max="1500"' in response.text
