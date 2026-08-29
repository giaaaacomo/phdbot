from pathlib import Path


async def test_health_ok(client):
    assert (await client.get("/health")).status_code == 200


def test_dashboard_source_contains_expected_controls():
    dashboard = (Path(__file__).parents[2] / "src/phd_searcher/static/index.html").read_text()
    assert "<title>PHDBOT — control panel</title>" in dashboard
    assert "<h1>PHDBOT</h1>" in dashboard
    assert "PhD Searcher" not in dashboard
    assert "pages/source" in dashboard
    assert "duration" in dashboard
    assert "Maximum results" in dashboard
    assert "Minimum pay" in dashboard
    assert "Minimum relevance" in dashboard
    assert "Detail pages" in dashboard
    assert "Related universities" in dashboard
    assert "Candidate screening" in dashboard
    assert 'review2: ["Deep review"' in dashboard
    assert "const stageLabel = stage" in dashboard
    assert 'title="Continue the interrupted run from its durable checkpoint"' in dashboard
    assert 'title="Exclude results below this semantic similarity threshold"' in dashboard
    assert "review-filter-tile" in dashboard
    assert "data-review-status" in dashboard
    assert 'aria-pressed="${status === value}"' in dashboard
    assert "country-mark" in dashboard
    assert "countryMark(u.country)" in dashboard
    assert 'title="${esc(countryName(normalized))}"' in dashboard
    assert 'data-cov-sort="tier"' in dashboard
    assert 'data-cov-sort="discovery"' in dashboard
    assert 'data-cov-sort="pages"' in dashboard
    assert "updateCoverageSortHeaders" in dashboard
    assert "max-width: 3440px" in dashboard
    assert "#pipeline.active { display: grid" in dashboard
    assert "#search .filter-layout { grid-template-columns" in dashboard
    assert "@media (min-width: 1700px)" in dashboard
    assert "#cov-tiles { grid-template-columns: repeat(6" in dashboard
    assert "#review-tiles { grid-template-columns: repeat(5" in dashboard
    assert "@media (min-width: 2200px)" in dashboard
    assert "max-width: 100ch; margin-inline: auto" in dashboard
    assert "grid-template-columns: repeat(2, minmax(0, 100ch))" in dashboard
    assert "numbered-hit" in dashboard
    assert "page.offset + index + 1" in dashboard
    assert '<th class="row-number"' in dashboard
    assert "Searching indexed opportunities" in dashboard
    assert 'SEARCH_HISTORY_KEY = "phdbot.searchHistory.v1"' in dashboard
    assert "Recent / frequent:" not in dashboard
    assert "query-history-clear" not in dashboard
    assert "selectedSearchPills.join(\" + \")" in dashboard
    assert 'button.setAttribute("aria-pressed", String(selected))' in dashboard
    assert "toggleSearchPill(entry.query)" in dashboard
    assert "button.query-pill.active" in dashboard
    assert "each part is searched separately" in dashboard
    assert "Filter data unavailable" in dashboard
    assert "const hasIncomeFilter" in dashboard
    assert "(raw: ${p.deadline_raw" not in dashboard
    assert 'aria-busy", String(active)' in dashboard
    assert "finally { setSearchLoading(false); }" in dashboard
    assert 'RESULT_VIEWS_KEY = "phdbot.resultViews.v1"' in dashboard
    assert 'id="s-mode"' in dashboard
    assert 'value="include_probable"' in dashboard
    assert 'id="s-max-uncertainty" type="number" min="0" max="100" step="5" value="60"' in dashboard
    assert "Verification evidence unavailable" in dashboard
    assert 'REPORTED_POSITIONS_KEY = "phdbot.reportedPositions.v1"' in dashboard
    assert "submitPositionFeedback" in dashboard
    assert "undoPositionFeedback" in dashboard
    assert ".verification-badge.probable" in dashboard
    assert 'markResultViewed(id, "details")' in dashboard
    assert "Source opened" in dashboard
    assert 'value === "phd"' in dashboard
    assert "PhD / doctoral / predoctoral position" in dashboard
    assert 'role="tablist"' in dashboard
    assert 'TAB_KEY = "phdbot.activeTab.v1"' in dashboard
    assert "Loading candidates" in dashboard
    assert "Filters reset; semantic query preserved" in dashboard
    assert "coverageLoadToken" in dashboard
    assert "reviewLoadToken" in dashboard
    assert "Stop the current run safely" in dashboard
    assert 'data-tab="review"' in dashboard
    assert 'data-tab="macros"' in dashboard
    assert "Refine automatic sample" in dashboard
    assert 'id="btn-export"' in dashboard
    assert "Save current search as macro" in dashboard
    assert 'id="p-schedule-at" type="datetime-local"' in dashboard
    assert 'id="btn-schedule"' in dashboard
    assert 'target:"pipeline", run_at:runAt, timezone:"Europe/Rome"' in dashboard
    assert "pipelineBodyFromControls" in dashboard
    assert "scheduleMacro" in dashboard
    assert "/v1/schedules?limit=100" in dashboard
    assert 'class="act ghost" id="btn-stop" disabled' in dashboard
    assert 'classList.toggle("danger", canStop)' in dashboard
    assert 'classList.toggle("ghost", resumable)' in dashboard
    assert "Dates and compensation" in dashboard
    assert "Relevance and result order" in dashboard
    assert "institutions" in dashboard
    assert "Collect &amp; publish" in dashboard
    assert "until the first empty page" in dashboard
    assert 'id="p-max-pages" type="number" min="1" max="1500"' in dashboard
