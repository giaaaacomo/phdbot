import pytest

from phd_searcher.pipeline.source_family import (
    FamilyCounts,
    FamilyDirection,
    family_direction,
    source_family_audit,
    source_family_keys,
    wilson_interval,
)


def test_numeric_and_uuid_detail_urls_share_a_scoped_template():
    numeric = source_family_keys(
        "https://jobs.example.test/vacancies/123456?utm_source=mail",
        listing_page_id=7,
    )
    other = source_family_keys(
        "https://jobs.example.test/vacancies/987654?fbclid=noise",
        listing_page_id=7,
    )
    assert numeric[0] == other[0] == "listing:7|template:jobs.example.test/vacancies/{id}"

    uuid = source_family_keys(
        "https://jobs.example.test/posting/550e8400-e29b-41d4-a716-446655440000",
        listing_page_id=8,
    )
    assert uuid[0] == "listing:8|template:jobs.example.test/posting/{uuid}"


def test_human_slugs_get_a_weaker_parent_route_without_collapsing_root_pages():
    keys = source_family_keys(
        "https://www.example.test/jobs/phd-in-naval-engineering",
        listing_page_id=4,
    )
    assert keys == (
        "listing:4|template:www.example.test/jobs/phd-in-naval-engineering",
        "listing:4|route:www.example.test/jobs/{item}",
    )
    assert source_family_keys("https://www.example.test/about", listing_page_id=4) == (
        "listing:4|template:www.example.test/about",
    )


def test_sources_and_subdomains_never_share_reputation_implicitly():
    first = source_family_keys("https://jobs.example.test/vacancy/42", listing_page_id=1)
    second = source_family_keys("https://jobs.example.test/vacancy/43", listing_page_id=2)
    tenant = source_family_keys("https://other.example.test/vacancy/42", listing_page_id=1)
    assert first[0] != second[0]
    assert first[0] != tenant[0]


def test_identifier_queries_are_templated_but_tracking_values_are_discarded():
    first = source_family_keys(
        "https://careers.example.test/show?jobId=ABC-123&lang=en&utm_medium=email",
        listing_page_id=3,
    )
    second = source_family_keys(
        "https://careers.example.test/show?jobId=XYZ-999&lang=de&utm_medium=social",
        listing_page_id=3,
    )
    assert first == second
    assert first[0].endswith("/show?jobid={value}")


def test_inline_fragments_share_a_family_only_with_their_listing():
    keys = source_family_keys(
        "https://example.test/open-positions#position-deadbeef",
        listing_url="https://example.test/open-positions",
        listing_page_id=9,
    )
    assert keys[0] == "listing:9|template:example.test/open-positions#item"
    assert keys[-1] == "listing:9|inline:example.test/open-positions#item"


@pytest.mark.parametrize("url", ["", "mailto:test@example.test", "https:///jobs/1"])
def test_invalid_or_non_http_urls_have_no_family(url: str):
    assert source_family_keys(url, listing_page_id=1) == ()


def test_wilson_interval_and_family_direction_are_conservative():
    low, high = wilson_interval(20, 20)
    assert low == pytest.approx(0.8389, rel=1e-3)
    assert high == pytest.approx(1.0)
    assert family_direction(FamilyCounts(opportunities=20)) == FamilyDirection.SUPPORTS_OPPORTUNITY
    assert family_direction(FamilyCounts(non_opportunities=20)) == FamilyDirection.SUPPORTS_NON_OPPORTUNITY
    assert family_direction(FamilyCounts(opportunities=6, non_opportunities=6)) == FamilyDirection.MIXED
    assert family_direction(FamilyCounts(opportunities=11)) == FamilyDirection.INSUFFICIENT


def test_leave_one_out_counts_never_go_negative():
    counts = FamilyCounts(opportunities=1, non_opportunities=1)
    assert counts.excluding(True) == FamilyCounts(non_opportunities=1)
    assert counts.excluding(False) == FamilyCounts(opportunities=1)
    assert FamilyCounts().excluding(True) == FamilyCounts()


def test_immutable_count_arithmetic_validates_subsets():
    counts = FamilyCounts().adding(True, 3).adding(False, 2)
    assert counts == FamilyCounts(opportunities=3, non_opportunities=2)
    assert counts.subtracting(FamilyCounts(opportunities=1, non_opportunities=2)) == FamilyCounts(
        opportunities=2
    )
    with pytest.raises(ValueError, match="subset"):
        counts.subtracting(FamilyCounts(opportunities=4))
    with pytest.raises(ValueError, match="non-negative"):
        counts.adding(True, -1)


def test_audit_metadata_is_versioned_and_family_keys_are_bounded():
    metadata = source_family_audit(f"https://example.test/jobs/{'x' * 800}", 17)
    assert metadata["source_family_version"] == "url-family-v1"
    keys = metadata["source_family_keys"]
    assert isinstance(keys, list)
    assert keys
    assert all(len(key) <= 512 for key in keys)
