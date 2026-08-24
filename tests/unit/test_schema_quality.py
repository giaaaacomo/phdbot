from phd_searcher.pipeline.schema_quality import (
    SCHEMA_NAVIGATION_BASE,
    SCHEMA_NAVIGATION_TITLE,
    SCHEMA_WILDCARD_BASE,
    schema_quality_issues,
)


def test_wildcard_document_schema_is_rejected():
    assert schema_quality_issues(
        {
            "baseSelector": "*",
            "fields": [{"name": "title", "selector": "h1", "type": "text"}],
        }
    ) == (SCHEMA_WILDCARD_BASE,)


def test_navigation_schema_is_rejected_even_when_css_is_formally_valid():
    assert schema_quality_issues(
        {
            "baseSelector": ".nav-item",
            "fields": [{"name": "title", "selector": ".nav-link", "type": "text"}],
        }
    ) == (SCHEMA_NAVIGATION_BASE, SCHEMA_NAVIGATION_TITLE)


def test_job_specific_navigation_name_is_not_rejected_by_keyword_alone():
    assert schema_quality_issues(
        {
            "baseSelector": ".job-navigation__position",
            "fields": [{"name": "title", "selector": ".job-navigation__title", "type": "text"}],
        }
    ) == ()


def test_specific_minimal_list_schema_is_left_to_result_quality_gate():
    assert schema_quality_issues(
        {
            "baseSelector": "#content .vacancies > li",
            "fields": [
                {"name": "title", "selector": "a", "type": "text"},
                {"name": "url", "selector": "a", "type": "attribute", "attribute": "href"},
            ],
        }
    ) == ()
