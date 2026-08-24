from datetime import datetime

import pytest

from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.pipeline.quality_gate import (
    CandidateAssessment,
    ListingHealthStatus,
    QualityDecision,
    QualityDisposition,
    QualityReason,
    _apply_decision,
    _apply_listing_health,
    _recover_clean_candidate,
    aggregate_listing_health,
    assess_candidate,
    inspect_candidate,
    summarize_listing,
    summarize_url_families,
)
from phd_searcher.pipeline.schema_quality import SCHEMA_NAVIGATION_BASE


def _assessment(
    position_id: int,
    disposition: QualityDisposition,
    reason: QualityReason | None = None,
    *,
    listing_page_id: int = 7,
    source_signals: tuple[QualityReason, ...] = (),
) -> CandidateAssessment:
    return CandidateAssessment(
        position_id=position_id,
        listing_page_id=listing_page_id,
        listing_url=f"https://example.test/listing/{listing_page_id}",
        decision=QualityDecision(disposition, (reason,) if reason else ()),
        source_signals=source_signals,
    )


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("javascript:alert(1)", QualityReason.INVALID_URL_SCHEME),
        ("//example.test/jobs/1", QualityReason.INVALID_URL_SCHEME),
        ("https:///jobs/1", QualityReason.INVALID_URL_HOST),
        ("https://exa mple.test/jobs/1", QualityReason.INVALID_URL_HOST),
        ("https://example.test:invalid/jobs/1", QualityReason.INVALID_URL_SYNTAX),
    ],
)
def test_invalid_http_url_is_quarantined(url: str, reason: QualityReason):
    decision = inspect_candidate(title="PhD position in naval engineering", url=url)
    assert decision.disposition == QualityDisposition.QUARANTINE
    assert reason in decision.reasons


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_http_and_https_candidate_passes_quality_gate(scheme: str):
    decision = inspect_candidate(
        title="Doctoral researcher in human-computer interaction",
        url=f"{scheme}://jobs.example.test/vacancy/42",
    )
    assert decision == QualityDecision(QualityDisposition.PASS)


@pytest.mark.parametrize(
    "title",
    [
        '<meta property="og:title" content="University">',
        '&lt;script src="/bundle.js"&gt;&lt;/script&gt;',
        '<link rel="stylesheet" href="/main.css">',
        "<div class='navigation'>Jobs</div>",
    ],
)
def test_html_in_title_is_quarantined(title: str):
    decision = inspect_candidate(title=title, url="https://example.test/jobs/1")
    assert decision.disposition == QualityDisposition.QUARANTINE
    assert QualityReason.TITLE_HTML_MARKUP in decision.reasons


def test_html_markup_appended_to_valid_url_is_quarantined():
    decision = inspect_candidate(
        title="NEMZETI KÖZSZOLGÁLATI EGYETEM",
        url="https://www.uni-nke.hu/calls/%3Cmeta%20charset=%22utf-8%22/%3E",
    )
    assert decision.disposition == QualityDisposition.QUARANTINE
    assert QualityReason.URL_HTML_MARKUP in decision.reasons


@pytest.mark.parametrize(
    "title",
    [
        "window.dataLayer = window.dataLayer || [];",
        "const config = {locale: 'en'};",
        "function init() { document.querySelector('#app'); }",
        '"@context": "https://schema.org", "@type": "JobPosting"',
    ],
)
def test_script_fragments_in_title_are_quarantined(title: str):
    decision = inspect_candidate(title=title, url="https://example.test/jobs/1")
    assert decision.disposition == QualityDisposition.QUARANTINE
    assert QualityReason.TITLE_SCRIPT in decision.reasons


@pytest.mark.parametrize(
    ("title", "url", "reason"),
    [
        ("app.bundle.js", "https://example.test/jobs/1", QualityReason.TITLE_ASSET_NAME),
        ("Open positions", "https://example.test/static/logo.svg", QualityReason.ASSET_URL),
        ("!!!!!!!!!!!!", "https://example.test/jobs/1", QualityReason.TITLE_ABSURD),
        ("PhD\x00position", "https://example.test/jobs/1", QualityReason.TITLE_CONTROL_CHARS),
    ],
)
def test_assets_and_absurd_titles_are_quarantined(title: str, url: str, reason: QualityReason):
    decision = inspect_candidate(title=title, url=url)
    assert decision.disposition == QualityDisposition.QUARANTINE
    assert reason in decision.reasons


def test_navigation_is_rejected_but_not_quarantined():
    decision = inspect_candidate(title="Read more", url="https://example.test/about")
    assert decision.disposition == QualityDisposition.REJECT
    assert decision.reasons == (QualityReason.NAVIGATION_TITLE,)


def test_html_in_description_alone_does_not_quarantine_legitimate_candidate():
    decision = inspect_candidate(
        title="PhD position in JavaScript program analysis",
        url="https://example.test/jobs/javascript-phd",
        description="<p>Study the expression x < y in generated programs.</p>",
    )
    assert decision.disposition == QualityDisposition.PASS


def test_listing_health_quarantines_systematically_broken_extraction():
    assessments = [
        *[_assessment(index, QualityDisposition.QUARANTINE, QualityReason.TITLE_HTML_MARKUP) for index in range(1, 6)],
        *[_assessment(index, QualityDisposition.PASS) for index in range(6, 21)],
    ]
    health = summarize_listing(assessments)
    assert health.status == ListingHealthStatus.QUARANTINE
    assert (health.total, health.passed, health.quarantined, health.rejected) == (20, 15, 5, 0)
    assert health.reason_counts == {"title_html_markup": 5}
    assert health.issue_ratio == pytest.approx(0.25)


def test_listing_health_degrades_for_isolated_artifact_without_quarantining_source():
    assessments = [
        _assessment(1, QualityDisposition.QUARANTINE, QualityReason.ASSET_URL),
        *[_assessment(index, QualityDisposition.PASS) for index in range(2, 21)],
    ]
    assert summarize_listing(assessments).status == ListingHealthStatus.DEGRADED


def test_navigation_heavy_listing_is_quarantined_at_source_level():
    assessments = [
        *[_assessment(index, QualityDisposition.REJECT, QualityReason.NAVIGATION_TITLE) for index in range(1, 11)],
        *[_assessment(index, QualityDisposition.PASS) for index in range(11, 21)],
    ]
    health = summarize_listing(assessments)
    assert health.status == ListingHealthStatus.QUARANTINE
    assert health.rejected == 10
    assert health.quarantined == 0


def test_cross_section_study_links_are_only_weak_signals_per_candidate():
    assessment = assess_candidate(
        position_id=1,
        listing_page_id=7,
        listing_url="https://www.tu-dortmund.de/karriere/offene-stellen/",
        title="Bachelor",
        url="https://www.tu-dortmund.de/studierende/studienangebot/bachelor/",
    )
    assert assessment.decision == QualityDecision(QualityDisposition.PASS)
    assert assessment.source_signals == (QualityReason.CONTENT_SECTION_MISMATCH,)


def test_same_study_section_does_not_penalize_legitimate_phd_project_catalogue():
    assessment = assess_candidate(
        position_id=1,
        listing_page_id=7,
        listing_url="https://example.test/study/postgraduate/phd-opportunities/",
        title="Development of resilient marine structures",
        url="https://example.test/study/postgraduate/projects/resilient-marine-structures/",
    )
    assert assessment.decision == QualityDecision(QualityDisposition.PASS)
    assert assessment.source_signals == ()


def test_explicit_vacancy_in_news_section_is_not_treated_as_editorial_noise():
    assessment = assess_candidate(
        position_id=1,
        listing_page_id=7,
        listing_url="https://example.test/news/",
        title="PhD vacancy in naval engineering",
        url="https://example.test/news/naval-engineering-project/",
    )
    assert assessment.decision == QualityDecision(QualityDisposition.PASS)
    assert assessment.source_signals == ()


def test_editorial_archive_is_quarantined_only_when_noise_is_systematic():
    noisy = [
        _assessment(
            index,
            QualityDisposition.PASS,
            source_signals=(QualityReason.EDITORIAL_ARCHIVE_ITEM,),
        )
        for index in range(1, 21)
    ]
    health = summarize_listing([*noisy, *[_assessment(index, QualityDisposition.PASS) for index in range(21, 26)]])
    assert health.status == ListingHealthStatus.QUARANTINE
    assert health.suspected == 20
    assert health.issue_ratio == pytest.approx(0.8)
    assert health.reason_counts == {"editorial_archive_item": 20}


def test_isolated_editorial_item_degrades_but_does_not_quarantine_source():
    health = summarize_listing(
        [
            _assessment(
                1,
                QualityDisposition.PASS,
                source_signals=(QualityReason.EDITORIAL_ARCHIVE_ITEM,),
            ),
            *[_assessment(index, QualityDisposition.PASS) for index in range(2, 21)],
        ]
    )
    assert health.status == ListingHealthStatus.DEGRADED
    assert health.suspected == 1


def test_structurally_unsafe_schema_forces_reversible_source_quarantine():
    health = summarize_listing(
        [_assessment(1, QualityDisposition.PASS)],
        source_reasons=(SCHEMA_NAVIGATION_BASE,),
    )
    assert health.status == ListingHealthStatus.QUARANTINE
    assert health.reason_counts == {SCHEMA_NAVIGATION_BASE: 1}


def test_healthy_listing_and_cross_listing_aggregation_are_stable():
    assessments = [
        _assessment(3, QualityDisposition.PASS, listing_page_id=9),
        _assessment(1, QualityDisposition.PASS, listing_page_id=7),
        _assessment(2, QualityDisposition.PASS, listing_page_id=7),
    ]
    health = aggregate_listing_health(assessments)
    assert [item.listing_page_id for item in health] == [7, 9]
    assert all(item.status == ListingHealthStatus.HEALTHY for item in health)


def test_url_family_metrics_group_siblings_without_changing_their_verdicts():
    assessments = [
        assess_candidate(
            position_id=index,
            listing_page_id=7,
            listing_url="https://example.test/jobs",
            title=f"Doctoral researcher {index}",
            url=f"https://example.test/jobs/{index}",
        )
        for index in range(1000, 1003)
    ]
    metrics = summarize_url_families(assessments)
    family = metrics["families"]["listing:7|template:example.test/jobs/{id}"]
    assert family == {
        "total": 3,
        "passed": 3,
        "rejected": 0,
        "quarantined": 0,
        "suspected": 0,
        "issue_ratio": 0.0,
        "reason_counts": {},
    }
    assert all(row.decision.disposition == QualityDisposition.PASS for row in assessments)


def test_url_family_metrics_are_bounded_and_serialized_with_listing_health():
    assessments = [
        CandidateAssessment(
            position_id=index,
            listing_page_id=7,
            listing_url="https://example.test/jobs",
            decision=QualityDecision(QualityDisposition.PASS),
            url_family_keys=(f"listing:7|route:example.test/family-{index % 3}/{{item}}",),
        )
        for index in range(9)
    ]
    metrics = summarize_url_families(assessments, maximum_families=2)
    assert metrics["repeated_keys"] == 3
    assert metrics["reported_keys"] == 2
    assert metrics["truncated"] is True

    serialized = summarize_listing(assessments).as_dict()
    assert serialized["url_family_metrics"]["version"] == "url-family-v1"


def test_summarize_listing_rejects_mixed_or_empty_input():
    with pytest.raises(ValueError, match="at least one"):
        summarize_listing([])
    with pytest.raises(ValueError, match="different listing"):
        summarize_listing(
            [
                _assessment(1, QualityDisposition.PASS, listing_page_id=7),
                _assessment(2, QualityDisposition.PASS, listing_page_id=8),
            ]
        )


def test_apply_decision_preserves_row_and_encodes_quarantine_separately_from_reject():
    position = Position(
        id=12,
        url="https://example.test/main.js",
        title="main.js",
        description="raw scraped data remains here",
        screening_status="review",
        screening_manual=False,
        screening_source="rules",
    )
    decision = QualityDecision(
        QualityDisposition.QUARANTINE,
        (QualityReason.ASSET_URL, QualityReason.TITLE_ASSET_NAME),
    )
    now = datetime(2026, 8, 1, 12, 0, 0)
    _apply_decision(position, decision, now=now)

    assert position.screening_status == "quarantine"
    assert position.screening_decision == "quarantine"
    assert position.screening_source == "quality_gate"
    assert position.screening_reason == "quality_gate:quarantine:asset_url,title_asset_name"
    assert position.description == "raw scraped data remains here"
    assert position.is_active is None  # il gate non nasconde ne' elimina la riga
    assert position.screened_at == now
    assert position.review_state == "source_broken"
    assert position.routing_reason == position.screening_reason


def test_apply_pass_is_a_noop():
    position = Position(
        id=13,
        url="https://example.test/jobs/13",
        title="Research assistantship",
        description="",
        screening_status="review",
        screening_reason="existing",
        screening_source="llm",
    )
    _apply_decision(position, QualityDecision(QualityDisposition.PASS), now=datetime(2026, 8, 1))
    assert position.screening_status == "review"
    assert position.screening_reason == "existing"
    assert position.screening_source == "llm"


def test_apply_pass_waits_for_clean_source_before_releasing_quarantine():
    position = Position(
        id=14,
        url="https://example.test/jobs/14",
        title="Doctoral researcher",
        description="Applications are open.",
        screening_status="quarantine",
        screening_reason="quality_gate:quarantine:source_health_quarantine",
        screening_source="quality_gate",
        screening_decision="quarantine",
        screening_confidence=1.0,
        review_state="source_broken",
    )
    now = datetime(2026, 8, 2)
    _apply_decision(position, QualityDecision(QualityDisposition.PASS), now=now)

    assert position.screening_status == "quarantine"
    assert position.screening_source == "quality_gate"
    assert position.screening_decision == "quarantine"
    assert position.review_state == "source_broken"


@pytest.mark.parametrize("previous_status", ["quarantine", "rejected"])
def test_clean_source_recovers_previous_quality_gate_outcome_for_retriage(previous_status: str):
    position = Position(
        id=14,
        url="https://example.test/jobs/14",
        title="Doctoral researcher",
        description="Applications are open.",
        screening_status=previous_status,
        screening_reason=f"quality_gate:{previous_status}:old_reason",
        screening_source="quality_gate",
        screening_decision=previous_status,
        screening_confidence=1.0,
        screening_evidence='["reason_code:old_reason"]',
        screening_model="old-model",
        screening_version="old-version",
        screening_manual=False,
        review_state="source_broken" if previous_status == "quarantine" else "resolved",
    )
    now = datetime(2026, 8, 2)

    recovered = _recover_clean_candidate(
        position,
        QualityDecision(QualityDisposition.PASS),
        now=now,
    )

    assert recovered is True
    assert position.screening_status == "pending"
    assert position.screening_reason == "quality_gate:recovered_clean_source"
    assert position.screening_source == "rules"
    assert position.screening_decision is None
    assert position.screening_confidence is None
    assert position.screening_evidence is None
    assert position.screening_model is None
    assert position.screening_version is None
    assert position.review_state == "untriaged"
    assert position.routing_reason == "quality_gate:source_recovered"
    assert position.screened_at == now


@pytest.mark.parametrize(
    ("screening_manual", "screening_source", "decision"),
    [
        (True, "quality_gate", QualityDisposition.PASS),
        (False, "llm", QualityDisposition.PASS),
        (False, "quality_gate", QualityDisposition.REJECT),
    ],
)
def test_recovery_preserves_manual_external_and_still_broken_outcomes(
    screening_manual: bool,
    screening_source: str,
    decision: QualityDisposition,
):
    position = Position(
        id=15,
        url="https://example.test/jobs/15",
        title="Read more" if decision == QualityDisposition.REJECT else "Doctoral researcher",
        description="",
        screening_status="rejected",
        screening_reason="existing",
        screening_source=screening_source,
        screening_decision="rejected",
        screening_manual=screening_manual,
        review_state="resolved",
    )

    recovered = _recover_clean_candidate(
        position,
        QualityDecision(decision),
        now=datetime(2026, 8, 2),
    )

    assert recovered is False
    assert position.screening_status == "rejected"
    assert position.screening_reason == "existing"
    assert position.screening_source == screening_source
    assert position.review_state == "resolved"


def test_apply_decision_never_overwrites_manual_outcome():
    position = Position(
        id=16,
        url="https://example.test/main.js",
        title="main.js",
        description="",
        screening_status="eligible",
        screening_reason="manual:confirmed",
        screening_source="manual",
        screening_decision="eligible",
        screening_manual=True,
    )

    _apply_decision(
        position,
        QualityDecision(QualityDisposition.QUARANTINE, (QualityReason.ASSET_URL,)),
        now=datetime(2026, 8, 2),
    )

    assert position.screening_status == "eligible"
    assert position.screening_reason == "manual:confirmed"
    assert position.screening_source == "manual"


def test_apply_listing_health_marks_quarantined_schema_stale_without_deleting_it():
    page = ListingPage(
        id=7,
        url="https://example.test/jobs",
        extraction_schema={"baseSelector": "*"},
        schema_status="ok",
    )
    health = summarize_listing(
        [
            *[
                _assessment(index, QualityDisposition.QUARANTINE, QualityReason.TITLE_HTML_MARKUP)
                for index in range(1, 6)
            ],
            *[_assessment(index, QualityDisposition.PASS) for index in range(6, 21)],
        ]
    )
    now = datetime(2026, 8, 1, 12, 0, 0)
    _apply_listing_health(page, health, now=now)

    assert page.quality_status == "quarantine"
    assert page.quality_reason == "quality_gate:quarantine:title_html_markup:5"
    assert page.quality_metrics["quarantined"] == 5
    assert page.quality_checked_at == now
    assert page.extraction_schema == {"baseSelector": "*"}
    assert page.schema_status == "stale"


def test_healthy_listing_clears_quality_reason_without_claiming_schema_was_regenerated():
    page = ListingPage(
        id=8,
        url="https://example.test/jobs",
        extraction_schema={"baseSelector": "*"},
        schema_status="stale",
        quality_status="quarantine",
        quality_reason="quality_gate:quarantine:title_html_markup:5",
    )
    health = summarize_listing([_assessment(1, QualityDisposition.PASS, listing_page_id=8)])

    _apply_listing_health(page, health, now=datetime(2026, 8, 2))

    assert page.quality_status == "healthy"
    assert page.quality_reason is None
    assert page.schema_status == "stale"
