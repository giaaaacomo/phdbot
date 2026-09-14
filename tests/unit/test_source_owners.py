import pytest
from sqlalchemy.dialects.postgresql import dialect, insert

from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.pipeline.discovery import _member_source_upsert
from phd_searcher.pipeline.source_owners import SourceOwners


def test_member_and_job_subdomain_are_owned_by_catalogue_not_consortium():
    owners = SourceOwners([(1, "https://consortium.example"), (2, "https://institute.example/en"),
                           (3, "https://institute.example/labs/specific")])
    assert owners.resolve("https://jobs.institute.example/openings") == 2
    assert owners.resolve("https://www.institute.example/careers") == 2
    assert owners.resolve("https://institute.example/labs/specific/jobs") == 3
    assert owners.resolve("https://institute.example/labs/specific-other/jobs") == 2
    assert owners.resolve("https://other.example/jobs") is None


def test_exact_lab_host_beats_parent_and_ambiguous_catalogue_abstains():
    owners = SourceOwners([(1, "https://cnr.example"), (2, "https://isti.cnr.example/it/")])
    assert owners.resolve("https://isti.cnr.example/en/announcements/jobs") == 2
    ambiguous = SourceOwners([(1, "https://shared.example"), (2, "https://www.shared.example/en")])
    assert ambiguous.resolve("https://shared.example/jobs") is None


@pytest.mark.parametrize("url", ["https://institute.example.evil.test/jobs", "file:///institute.example/jobs",
                                   "https://user:pass@institute.example/jobs", "https://institute.example:999/jobs"])
def test_untrusted_urls_do_not_resolve(url):
    assert SourceOwners([(1, "https://institute.example")]).resolve(url) is None


def test_path_scoped_institution_does_not_claim_unrelated_jobs():
    owners = SourceOwners([(1, "https://shared.example/lab-a")])
    assert owners.resolve("https://shared.example/lab-b/jobs") is None
    assert owners.resolve("https://jobs.shared.example/lab-a") is None
    assert owners.resolve("https://shared.example/lab-a/jobs") == 1


def test_reassignment_only_checks_this_unused_source_and_retains_existing_data():
    stmt = _member_source_upsert(insert(ListingPage).values(url="https://institute.example/jobs"), 1, 2)
    sql = str(stmt.compile(dialect=dialect()))
    assert "listing_pages.last_scraped_at IS NULL" in sql
    assert "listing_pages.schema_status =" in sql
    assert "listing_pages.university_id =" in sql
    assert "FROM positions, listing_pages" not in sql
    assert "positions.listing_page_id = listing_pages.id" in sql
    assert "DO UPDATE SET university_id =" in sql
