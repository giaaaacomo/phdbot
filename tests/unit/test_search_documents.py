from phd_searcher.engine.search_documents import (
    build_candidate_search_document,
    build_institution_search_document,
    clean_search_text,
)


def test_clean_search_text_removes_markup_and_truncates_on_word_boundary() -> None:
    assert clean_search_text(" <p>Design&nbsp; for   health</p> ") == "Design for health"
    assert clean_search_text("one two three", max_chars=8) == "one two…"
    assert clean_search_text("x < y and still visible") == "x < y and still visible"
    assert clean_search_text("abcdef", max_chars=3) == "ab…"
    assert len(clean_search_text("abcdef", max_chars=3)) == 3


def test_candidate_document_is_compact_and_candidate_specific() -> None:
    document = build_candidate_search_document(
        title="XR researcher",
        position_type="phd",
        institution="Example University",
        description="<p>Short listing excerpt for interaction design.</p>",
        description_chars=25,
    )

    assert document.splitlines()[:3] == [
        "Title: XR researcher",
        "Position type: phd",
        "Institution: Example University",
    ]
    assert "Description: Short listing excerpt…" in document
    assert "<p>" not in document


def test_institution_document_keeps_names_and_representative_snippets_bounded() -> None:
    document = build_institution_search_document(
        name="Mixed Reality Lab",
        university="Example University",
        kind="research_group",
        text=(
            "Mixed Reality Lab\nExample University\n"
            "Virtual reality and spatial computing research\n"
            + "interaction design " * 100
        ),
        max_chars=180,
    )

    assert document.startswith(
        "Name: Mixed Reality Lab\n"
        "Institution: Example University\n"
        "Entity type: research_group"
    )
    assert "Virtual reality and spatial computing research" in document
    assert len(document) <= 180
    assert document.count("Mixed Reality Lab") == 1
