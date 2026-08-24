from phd_searcher.pipeline.urls import is_listing_page_url


def test_listing_page_url_accepts_html_routes():
    assert is_listing_page_url("https://uni.example/jobs/")
    assert is_listing_page_url("https://uni.example/vacancies.php?page=2")


def test_listing_page_url_rejects_documents_case_insensitively():
    assert not is_listing_page_url("https://uni.example/call.DOCX")
    assert not is_listing_page_url("https://uni.example/rules.pdf?download=1")
