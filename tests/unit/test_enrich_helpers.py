from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from phd_searcher.pipeline.enrich import (
    _DETAIL_FETCH_RETRY_VERSION,
    _DURABLE_EVIDENCE_STATES,
    _EURAXESS_DETAIL_DELAY,
    _EURAXESS_MAX_COOLDOWN,
    _EURAXESS_RATE_LIMIT_COOLDOWN,
    _LEGACY_REVALIDATION_VERSIONS,
    _apply_detail_screening,
    _apply_extracted_detail_metadata,
    _cooldown_seconds,
    _crawl_detail_document,
    _detail_selection_priority,
    _extract_fragment_document,
    _extract_pdf_text,
    _fragment_base_url,
    _has_attributable_fragment_evidence,
    _has_authoritative_screening,
    _has_sufficient_inline_evidence,
    _has_url_fragment,
    _is_access_block,
    _is_browser_download,
    _is_euraxess_url,
    _is_rate_limit,
    _is_supported_detail_url,
    _looks_like_direct_document,
    _looks_like_unsupported_asset,
    _needs_legacy_revalidation_evidence,
    _preserve_screening_for_detail,
    _UnsupportedDirectDocumentError,
)
from phd_searcher.pipeline.rule_sweep import RULE_SWEEP_VERSION


def test_euraxess_detail_urls_use_conservative_rate_limits():
    assert _is_euraxess_url("https://euraxess.ec.europa.eu/jobs/454148")
    assert _EURAXESS_DETAIL_DELAY >= 2
    assert _EURAXESS_RATE_LIMIT_COOLDOWN >= 300
    assert _EURAXESS_MAX_COOLDOWN >= 3600
    assert _is_rate_limit(RuntimeError("HTTP 429 Too Many Requests"))
    assert [_cooldown_seconds(value) for value in range(1, 7)] == [300, 600, 1200, 2400, 3600, 3600]


def test_other_hosts_do_not_inherit_euraxess_rate_limits():
    assert not _is_euraxess_url("https://example.test/jobs/454148")
    assert not _is_euraxess_url("https://euraxess.ec.europa.eu.example.test/jobs/454148")


def test_direct_document_urls_are_detected_without_classifying_normal_pages():
    assert _looks_like_direct_document("https://example.test/calls/call.pdf")
    assert _looks_like_direct_document("https://example.test/files/call/download")
    assert _looks_like_direct_document("https://example.test/file?id=1&download=true")
    assert not _looks_like_direct_document("https://example.test/jobs/1")


def test_only_http_urls_are_sent_to_the_detail_fetchers():
    assert _is_supported_detail_url("https://example.test/jobs/1")
    assert _is_supported_detail_url("http://example.test/jobs/1")
    assert not _is_supported_detail_url("mailto:doctoral.school@example.test")
    assert not _is_supported_detail_url("/relative/job/1")


def test_image_assets_are_not_sent_to_the_html_crawler():
    assert _looks_like_unsupported_asset("https://example.test/poster.JPG?download=1")
    assert _looks_like_unsupported_asset("https://example.test/call.svg")
    assert not _looks_like_unsupported_asset("https://example.test/call.pdf")


def test_access_blocks_are_not_retried_inside_the_same_run():
    assert _is_access_block("Blocked by anti-bot protection: HTTP 403")
    assert _is_access_block("Access denied: CAPTCHA required")
    assert not _is_access_block("Blocked by anti-bot protection: Structural: minimal_text")
    assert not _is_access_block("HTTP 503 temporary outage")


def test_browser_download_navigation_switches_to_direct_document_fetch():
    assert _is_browser_download(RuntimeError("Page.goto: Download is starting"))
    assert not _is_browser_download(RuntimeError("Page.goto: navigation timeout"))


def test_detail_retry_version_reopens_failures_after_download_fallback_changes():
    assert _DETAIL_FETCH_RETRY_VERSION == "v4"


def test_fragment_detail_metadata_extracts_deadline_and_terms():
    position = SimpleNamespace(
        compensation_raw=None,
        duration_raw=None,
        published_raw=None,
        published_at=None,
        deadline_raw=None,
        deadline=None,
        research_group=None,
    )

    _apply_extracted_detail_metadata(
        position,
        "Posted on: 10 July 2026\nDuration: 36 months\n"
        "Gross salary: 52,000 CHF per year\n"
        "Application Deadline: 15 Aug 2026 - 00:00 (UTC)\n"
        "Research group: Human-Centred Design Lab",
    )

    assert position.deadline == date(2026, 8, 15)
    assert position.deadline_raw.startswith(
        "Application Deadline: 15 Aug 2026 - 00:00 (UTC)"
    )
    assert position.duration_raw == "Duration: 36 months"
    assert position.compensation_currency == "CHF"
    assert position.research_group == "Human-Centred Design Lab"


def test_detail_metadata_preserves_an_existing_richer_deadline():
    position = SimpleNamespace(
        compensation_raw="existing salary",
        duration_raw="existing duration",
        published_raw="existing publication",
        published_at=date(2026, 7, 1),
        deadline_raw="existing detail deadline",
        deadline=date(2026, 12, 31),
        research_group="Existing Lab",
    )

    _apply_extracted_detail_metadata(
        position,
        "Application Deadline: 15 Aug 2026 - 00:00 (UTC)",
    )

    assert position.deadline == date(2026, 12, 31)
    assert position.deadline_raw == "existing detail deadline"


@pytest.mark.asyncio
async def test_failed_crawl_download_result_switches_to_direct_fetch(monkeypatch):
    crawler = SimpleNamespace(
        arun=AsyncMock(
            return_value=SimpleNamespace(
                success=False,
                error_message="Page.goto: Download is starting",
            )
        )
    )
    direct_fetch = AsyncMock(return_value="PDF evidence text")
    monkeypatch.setattr(
        "phd_searcher.pipeline.enrich._fetch_direct_document",
        direct_fetch,
    )

    result = await _crawl_detail_document(
        crawler,
        "https://example.test/file/call?version=1",
        SimpleNamespace(),
    )

    assert result == "PDF evidence text"
    direct_fetch.assert_awaited_once_with(
        "https://example.test/file/call?version=1"
    )


def test_pdf_extractor_rejects_non_pdf_payloads_without_retryable_parser_noise():
    with pytest.raises(_UnsupportedDirectDocumentError, match="not a PDF"):
        _extract_pdf_text(b"<html>not a PDF</html>")


def test_manual_and_llm_screening_survive_enrich_rules():
    assert _has_authoritative_screening(manual=True, source="manual", status="eligible")
    assert _has_authoritative_screening(manual=False, source="llm", status="eligible")
    assert _has_authoritative_screening(manual=False, source="llm", status="rejected")
    assert _has_authoritative_screening(manual=False, source="router", status="review")
    assert _has_authoritative_screening(manual=False, source="cache", status="eligible")
    assert not _has_authoritative_screening(manual=False, source="rules", status="eligible")
    assert not _has_authoritative_screening(manual=False, source="llm", status="pending")
    assert RULE_SWEEP_VERSION == "rules-v13"


def test_evidence_collection_never_rewrites_the_public_screening_verdict():
    assert _preserve_screening_for_detail(
        operation="evidence",
        manual=False,
        source="rules",
        status="eligible",
    )
    assert _preserve_screening_for_detail(
        operation="evidence",
        manual=False,
        source="rules",
        status="review",
    )
    assert not _preserve_screening_for_detail(
        operation="enrich",
        manual=False,
        source="rules",
        status="eligible",
    )


def test_legacy_hybrid_verdicts_are_revalidated_progressively_without_demotion():
    assert set(_LEGACY_REVALIDATION_VERSIONS) == {"hybrid-v2", "hybrid-v3", "hybrid-v4"}
    assert set(_DURABLE_EVIDENCE_STATES) == {
        "needs_evidence",
        "semantic_uncertain",
        "fetch_failed",
    }
    eligible = SimpleNamespace(
        id=20,
        screening_manual=False,
        screening_source="llm",
        screening_version="hybrid-v3",
        screening_status="eligible",
        review_state="resolved",
        position_type="phd",
    )
    rejected = SimpleNamespace(**{**eligible.__dict__, "id": 21, "screening_status": "rejected"})
    v4 = SimpleNamespace(**{**eligible.__dict__, "id": 24, "screening_version": "hybrid-v4"})
    current = SimpleNamespace(**{**eligible.__dict__, "id": 22, "screening_version": "hybrid-v5"})
    manual = SimpleNamespace(**{**eligible.__dict__, "id": 23, "screening_manual": True})

    assert _needs_legacy_revalidation_evidence(eligible)
    assert _needs_legacy_revalidation_evidence(rejected)
    assert _needs_legacy_revalidation_evidence(v4)
    assert not _needs_legacy_revalidation_evidence(current)
    assert not _needs_legacy_revalidation_evidence(manual)
    assert eligible.screening_status == "eligible"
    assert rejected.screening_status == "rejected"


def test_legacy_revalidation_and_explicit_evidence_routes_are_fetched_first():
    legacy = SimpleNamespace(
        id=40,
        screening_manual=False,
        screening_source="llm",
        screening_version="hybrid-v2",
        screening_status="eligible",
        review_state="resolved",
        position_type="other",
    )
    routed = SimpleNamespace(
        id=20,
        screening_manual=False,
        screening_source="router",
        screening_version="hybrid-v5",
        screening_status="review",
        review_state="needs_evidence",
        position_type="other",
    )
    typed = SimpleNamespace(**{**routed.__dict__, "id": 1, "review_state": "untriaged", "position_type": "phd"})
    ambient = SimpleNamespace(**{**routed.__dict__, "id": 2, "review_state": "untriaged"})

    ordered = sorted(
        (ambient, typed, routed, legacy),
        key=lambda position: _detail_selection_priority(position, operation="evidence"),
    )

    assert [position.id for position in ordered] == [40, 20, 1, 2]


def test_evidence_fetch_preserves_public_legacy_verdict_until_deep_review():
    position = SimpleNamespace(
        screening_status="eligible",
        position_type="phd",
        review_state="resolved",
        routing_reason="llm:legacy",
    )

    _apply_detail_screening(
        SimpleNamespace(),
        position,
        full_description="Applications are invited for a funded doctoral position.",
        classified="phd",
        promote_after_fetch=False,
        pipeline_run_id=47,
        evidence_route="evidence:detail_page",
    )

    assert position.screening_status == "eligible"
    assert position.review_state == "ready_deep_review"
    assert position.routing_reason == "evidence:detail_page"


def test_short_inline_fragment_is_not_treated_as_new_review_evidence():
    assert not _has_sufficient_inline_evidence("PhD position — apply now")
    assert _has_sufficient_inline_evidence("word " * 50)


def test_long_title_repeated_by_fragment_is_not_detail_evidence():
    title = "A very long research project title " * 6

    assert not _has_attributable_fragment_evidence(title, title)
    assert _has_attributable_fragment_evidence(
        f"{title} Applications are invited by 31 December for a funded doctoral position.",
        title,
    )


def test_substantial_fragment_text_without_its_title_is_not_attributable():
    unrelated_catalogue = (
        "Applications are invited for several unrelated projects. " + "evidence " * 40
    )

    assert not _has_attributable_fragment_evidence(
        unrelated_catalogue,
        "Target doctoral project",
    )


def test_every_url_fragment_uses_only_its_attributable_inline_evidence():
    assert _has_url_fragment("https://example.test/jobs#position-123")
    assert _has_url_fragment("https://example.test/doctoral-school#31278556-collapse6")
    assert not _has_url_fragment("https://example.test/jobs")
    assert _fragment_base_url("https://example.test/jobs?q=phd#call-1") == "https://example.test/jobs?q=phd"


def test_fragment_dom_extraction_follows_exact_aria_controls():
    html = """
    <button id="call-1" aria-controls="panel-1">PhD in naval engineering</button>
    <section id="panel-1"><p>Applications are invited for a funded doctoral position.</p>
    <p>Submit the application before 31 December 2030. The project investigates
    hydrodynamics, sustainable ship design and advanced numerical simulation.
    Candidates will join the naval engineering laboratory for three years.</p></section>
    <section id="panel-2">Unrelated vacancy</section>
    """

    text = _extract_fragment_document(
        html,
        fragment="call-1",
        title="PhD in naval engineering",
    )

    assert text is not None
    assert "funded doctoral position" in text
    assert "Unrelated vacancy" not in text


def test_fragment_dom_extraction_tokenizes_aria_controls_and_abstains_on_many_panels():
    body = "Applications are invited for a doctoral position. " + ("evidence " * 35)
    one_panel = (
        '<button id="call-1" aria-controls="panel-1">Call</button>'
        f'<section id="panel-1">{body}</section>'
        '<button aria-controls="panel-1 auxiliary-panel">Related control</button>'
    )
    many_panels = (
        '<button id="call-1" aria-controls="panel-1 panel-2">Call</button>'
        f'<section id="panel-1">{body}</section><section id="panel-2">{body}</section>'
    )

    assert _extract_fragment_document(
        one_panel, fragment="panel-1", title="Call"
    ) is not None
    assert _extract_fragment_document(
        many_panels, fragment="call-1", title="Call"
    ) is None


def test_synthetic_fragment_requires_one_unique_exact_title_container():
    body = "word " * 50
    html = f"<article><h2>Quantum project</h2><p>{body}</p></article>"
    duplicate = html + f"<article><h2>Quantum project</h2><p>{body}</p></article>"

    assert _extract_fragment_document(
        html,
        fragment="position-deadbeefdeadbeef",
        title="Quantum project",
    ) is not None
    assert _extract_fragment_document(
        duplicate,
        fragment="position-deadbeefdeadbeef",
        title="Quantum project",
    ) is None


def test_fragment_extraction_never_returns_the_whole_page_for_an_unknown_target():
    html = "<main>" + ("unrelated content " * 100) + "</main>"

    assert _extract_fragment_document(html, fragment="missing", title="Missing title") is None


def test_synthetic_fragment_never_promotes_an_unbounded_page_container():
    html = "<main><h2>Unique title</h2><p>" + ("unrelated page text " * 80) + "</p></main>"

    assert _extract_fragment_document(
        html,
        fragment="position-deadbeefdeadbeef",
        title="Unique title",
    ) is None


def test_synthetic_fragment_never_promotes_a_whole_bounded_listing():
    html = (
        '<div class="vacancies">'
        + "<section>Earlier vacancy "
        + ("unrelated listing text " * 90)
        + "</section>"
        + "<h2>Target vacancy</h2>"
        + ("candidate evidence " * 40)
        + "</div>"
    )

    assert _extract_fragment_document(
        html,
        fragment="position-deadbeefdeadbeef",
        title="Target vacancy",
    ) is None
