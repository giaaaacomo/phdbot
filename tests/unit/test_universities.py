"""Regression test per copertura e resilienza dello stadio università."""

from phd_searcher.pipeline.universities import (
    _CURATED_INSTITUTIONS,
    _EXCLUDED_INSTITUTION_IDS,
    _SPECIALIST_CLASSES,
    _SPECIALISTS_QUERY,
    _UNIS_QUERY,
    _WEBSITE_OVERRIDES,
)


def test_query_is_limited_to_universities() -> None:
    assert "wd:Q3918" in _UNIS_QUERY
    assert "wd:Q38723" not in _UNIS_QUERY
    assert all(f"wd:{item}" in _UNIS_QUERY for item in _EXCLUDED_INSTITUTION_IDS)


def test_specialist_query_has_conservative_quality_gates() -> None:
    assert "?u wdt:P31 ?specialistClass" in _SPECIALISTS_QUERY
    assert "wdt:P279*" not in _SPECIALISTS_QUERY
    assert "P6782" in _SPECIALISTS_QUERY  # ROR
    assert "P5584" in _SPECIALISTS_QUERY  # WHED
    assert "?sitelinks >= 2" in _SPECIALISTS_QUERY
    assert {
        "Q17028020",
        "Q1365560",
        "Q15850083",
        "Q1941786",
        "Q383092",
        "Q380093",
        "Q21540699",
        "Q184644",
    } <= _SPECIALIST_CLASSES.keys()
    rendered = _SPECIALISTS_QUERY.format(
        qid="Q38",
        specialist_classes=" ".join(f"wd:{item}" for item in _SPECIALIST_CLASSES),
    )
    assert "wd:Q38" in rendered
    assert "wd:Q184644" in rendered


def test_ecal_is_in_curated_institutions() -> None:
    ecal = next(item for item in _CURATED_INSTITUTIONS if item["wikidata_id"] == "Q3577724")
    assert ecal["country"] == "CH"
    assert ecal["website_url"] == "https://ecal.ch/en/"
    assert ecal["catalog_tier"] == "specialist"


def test_verified_specialist_gaps_are_curated() -> None:
    curated = {item["wikidata_id"]: item for item in _CURATED_INSTITUTIONS}
    assert curated["Q3128581"]["country"] == "CH"  # HEAD Geneve lacks ROR/WHED in Wikidata
    assert curated["Q2504327"]["country"] == "MC"  # only missing catalog country
    assert all(item["catalog_tier"] == "specialist" for item in curated.values())


def test_verified_stale_websites_are_overridden() -> None:
    assert _WEBSITE_OVERRIDES["Q11815245"].startswith("https://uczelniaoswiecim.edu.pl/")
    assert _WEBSITE_OVERRIDES["Q1782547"] == "https://cons.bz.it/it/"
