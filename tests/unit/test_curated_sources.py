from datetime import datetime
from importlib import import_module

import pytest
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.university import University
from phd_searcher.pipeline.curated_sources import (
    ETH_ZURICH_JOBS_SCHEMA,
    FBK_JOBS_SCHEMA,
    IMPERIAL_JOBS_SCHEMA,
    IMPERIAL_JOBS_URL,
    ISTI_CNR_CALLS_SCHEMA,
    TURKU_JOBS_SCHEMA,
    TURKU_JOBS_URL,
    seed_curated_sources,
)
from phd_searcher.pipeline.schema_quality import schema_quality_issues


def test_eth_curated_schema_extracts_an_official_job_card() -> None:
    html = """
    <ul>
      <li class="job-ad__item__wrapper">
        <a class="job-ad__item__link" href="/job/view/JOPG_ethz_example">
          <h3 class="job-ad__item__title">Interaction Designer</h3>
          <div class="job-ad__item__details">60%-100%, Zurich, fixed-term</div>
          <div class="job-ad__item__company">28.08.2026 | IVIA Lab</div>
        </a>
      </li>
    </ul>
    """

    assert schema_quality_issues(ETH_ZURICH_JOBS_SCHEMA) == ()
    items = JsonCssExtractionStrategy(ETH_ZURICH_JOBS_SCHEMA).extract("file:///eth", html)

    assert items == [
        {
            "title": "Interaction Designer",
            "url": "/job/view/JOPG_ethz_example",
            "description": "28.08.2026 | IVIA Lab",
            "area": "28.08.2026 | IVIA Lab",
            "duration": "60%-100%, Zurich, fixed-term",
            "published": "28.08.2026 | IVIA Lab",
            "research_group": "28.08.2026 | IVIA Lab",
        }
    ]


def test_isti_cnr_curated_schema_extracts_an_official_call_card() -> None:
    html = """
    <main class="content-category">
      <div class="list-group">
        <a class="list-group-item list-group-item-action"
           href="/it/comunicazioni/bandi/2683-bando-br-isti-012-2026-pi">
          <small>28-08-2026</small>
          <h5>Bando BR ISTI 012/2026 PI</h5>
          <p>Le domande devono pervenire entro il 14 settembre 2026.</p>
        </a>
      </div>
    </main>
    """

    assert schema_quality_issues(ISTI_CNR_CALLS_SCHEMA) == ()
    items = JsonCssExtractionStrategy(ISTI_CNR_CALLS_SCHEMA).extract(
        "https://www.isti.cnr.it/it/comunicazioni/bandi",
        html,
    )

    assert items == [
        {
            "url": "/it/comunicazioni/bandi/2683-bando-br-isti-012-2026-pi",
            "title": "Bando BR ISTI 012/2026 PI",
            "description": "Le domande devono pervenire entro il 14 settembre 2026.",
            "published": "28-08-2026",
            "deadline": "Le domande devono pervenire entro il 14 settembre 2026.",
            "area": "Le domande devono pervenire entro il 14 settembre 2026.",
        }
    ]


def test_fbk_curated_schema_extracts_an_official_job_row() -> None:
    html = """
    <table class="GRID">
      <tr class="GRID_HDR_ROW"><td>Title</td></tr>
      <tr class="GRID_DAT_ROW">
        <td data-title="Title"><a href="/Annunci/Jobs_Postdoc_123.htm">
          Postdoctoral Researcher in Early Modern History
        </a></td>
        <td data-title="Recruitment Type">PNRR</td>
        <td data-title="Date published">8/11/2026</td>
        <td data-title="Deadline">9/11/2026 11:55 PM</td>
      </tr>
    </table>
    """

    assert schema_quality_issues(FBK_JOBS_SCHEMA) == ()
    items = JsonCssExtractionStrategy(FBK_JOBS_SCHEMA).extract(
        "https://jobs.fbk.eu/",
        html,
    )

    assert items == [
        {
            "title": "Postdoctoral Researcher in Early Modern History",
            "url": "/Annunci/Jobs_Postdoc_123.htm",
            "description": "PNRR",
            "published": "8/11/2026",
            "deadline": "9/11/2026 11:55 PM",
        }
    ]


def test_imperial_uses_the_official_structured_talentlink_adapter() -> None:
    assert IMPERIAL_JOBS_URL.endswith("/jobs/search-jobs/")
    assert IMPERIAL_JOBS_SCHEMA["adapter"] == "talentlink"
    assert IMPERIAL_JOBS_SCHEMA["siteTechId"] == "PMMFK026203F3VBQB8NLOV4CQ"
    assert IMPERIAL_JOBS_SCHEMA["pageSize"] == 100


def test_turku_uses_the_feed_that_drives_its_official_vacancy_page() -> None:
    assert TURKU_JOBS_URL.endswith("/open-vacancies")
    assert TURKU_JOBS_SCHEMA["adapter"] == "talentadore"
    assert "ats.talentadore.com/positions/3VMfJS4/json" in str(
        TURKU_JOBS_SCHEMA["feedUrl"]
    )


def test_source_adapter_migration_invalidates_every_legacy_imperial_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = import_module(
        "phd_searcher.database.alembic.versions."
        "e3b7c1a9d620_isti_cnr_curated_source"
    )
    statements: list[str] = []

    class _Bind:
        def execute(self, statement: object, _params: object = None) -> None:
            statements.append(str(statement))

    monkeypatch.setattr(migration.op, "get_bind", lambda: _Bind())
    migration.upgrade()

    position_update = next(sql for sql in statements if "UPDATE positions" in sql)
    assert "https://www.imperial.ac.uk/jobs/" in position_update
    assert "https://www.imperial.ac.uk/jobs/career-programmes/phd-vacancies/" in position_update
    assert "https://www.imperial.ac.uk/jobs/search-jobs/" in position_update


class _ExistingSourceSession:
    def __init__(self, source: ListingPage) -> None:
        self.source = source

    async def scalar(self, _statement: object) -> ListingPage:
        return self.source

    def add(self, _value: object) -> None:
        raise AssertionError("an existing source must be updated, not inserted")


@pytest.mark.asyncio
async def test_changed_curated_schema_resets_stale_quality_state() -> None:
    university = University(id=7, wikidata_id="Q3803752", name="ISTI", country="IT")
    checked_at = datetime(2026, 8, 1)
    source = ListingPage(
        id=11,
        university_id=7,
        url="https://www.isti.cnr.it/it/comunicazioni/bandi",
        kind="university",
        source="seed",
        extraction_schema={"name": "old schema"},
        schema_status="ok",
        quality_status="quarantine",
        quality_reason="old failure",
        quality_metrics={"broken": 10},
        quality_checked_at=checked_at,
        last_scraped_at=checked_at,
    )

    assert await seed_curated_sources(_ExistingSourceSession(source), university) == 1  # type: ignore[arg-type]

    assert source.extraction_schema == ISTI_CNR_CALLS_SCHEMA
    assert source.quality_status == "unknown"
    assert source.quality_reason is None
    assert source.quality_metrics == {}
    assert source.quality_checked_at is None
    assert source.last_scraped_at is None


@pytest.mark.asyncio
async def test_unchanged_curated_schema_preserves_quality_history() -> None:
    university = University(id=7, wikidata_id="Q3803752", name="ISTI", country="IT")
    checked_at = datetime(2026, 8, 1)
    source = ListingPage(
        id=11,
        university_id=7,
        url="https://www.isti.cnr.it/it/comunicazioni/bandi",
        kind="university",
        source="seed",
        extraction_schema=ISTI_CNR_CALLS_SCHEMA,
        schema_status="ok",
        quality_status="healthy",
        quality_metrics={"accepted": 10},
        quality_checked_at=checked_at,
        last_scraped_at=checked_at,
    )

    await seed_curated_sources(_ExistingSourceSession(source), university)  # type: ignore[arg-type]

    assert source.quality_status == "healthy"
    assert source.quality_checked_at == checked_at
    assert source.last_scraped_at == checked_at
