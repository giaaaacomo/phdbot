from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

from phd_searcher.pipeline.curated_sources import ETH_ZURICH_JOBS_SCHEMA
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
