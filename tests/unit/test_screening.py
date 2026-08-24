import pytest

from phd_searcher.screening import (
    SCREENING_STATUSES,
    ScreeningDecision,
    detail_rejection_evidence,
    screen_enriched_position,
    screen_position,
)


def test_recognized_position_is_eligible():
    decision = screen_position(
        "PhD position in naval engineering",
        "https://example.test/jobs/1",
        position_type="phd",
    )
    assert decision.status == "eligible"
    assert decision.reason == "recognized_type:phd"


def test_recognized_type_without_vacancy_signal_requires_review():
    decision = screen_position(
        "Doctoral programme in naval engineering",
        "https://example.test/study/doctoral-programme",
        position_type="phd",
    )
    assert decision.status == "review"
    assert decision.reason == "recognized_type_without_vacancy_signal:phd"


def test_screening_statuses_include_quarantine():
    assert "quarantine" in SCREENING_STATUSES


def test_obvious_degree_pages_are_rejected_without_fetching():
    for title in (
        "Grado en Medicina",
        "Doble Grado en Arquitectura y Diseño",
        "Máster en Urbanismo",
        "Corso di laurea in Ingegneria",
        "Bachelor of Science in Physics",
    ):
        decision = screen_position(title, "https://example.test/study")
        assert decision.status == "rejected", title
        assert decision.reason == "degree_or_course_page"


def test_vacancy_signal_overrides_degree_words():
    decision = screen_position(
        "Master's degree required for research position",
        "https://example.test/jobs/2",
    )
    assert decision.status == "eligible"


def test_ambiguous_candidate_is_preserved_for_manual_review():
    decision = screen_position(
        "Doctoral programme in design",
        "https://example.test/doctoral/design",
    )
    assert decision.status == "review"
    assert decision.reason == "ambiguous_candidate"


def test_explicitly_unavailable_position_is_rejected_despite_phd_keyword():
    decision = screen_position(
        "No PhD positions currently available",
        "https://example.test/opportunities",
        position_type="phd",
    )
    assert decision.status == "rejected"
    assert decision.reason == "explicitly_closed_or_unavailable"


def test_explicit_status_closed_is_rejected_but_negation_is_not_inverted():
    closed = screen_position(
        "PhD call for applications ||Status: CLOSED",
        "https://example.test/opportunities/1",
        position_type="phd",
    )
    not_closed = screen_position(
        "PhD position ||Status: NOT CLOSED",
        "https://example.test/opportunities/2",
        position_type="phd",
    )

    assert closed.status == "rejected"
    assert closed.reason == "explicitly_closed_or_unavailable"
    assert not_closed.status == "eligible"


def test_open_predoctoral_offer_is_eligible():
    decision = screen_position(
        "Pre-doctoral place offer in artificial intelligence ||Status: OPEN",
        "https://example.test/opportunities/predoc-1",
        position_type="phd",
    )

    assert decision.status == "eligible"
    assert decision.reason == "recognized_type:phd"


def test_named_intern_role_is_a_vacancy_signal():
    decision = screen_position(
        "E-Commerce Intern",
        "https://example.test/opportunities/e-commerce",
        position_type="internship",
    )

    assert decision.status == "eligible"
    assert decision.reason == "recognized_type:internship"


def test_spanish_structured_closed_status_is_rejected():
    decision = screen_position(
        "Oferta de plaza predoctoral ||Estado: CERRADA",
        "https://example.test/oportunidades/1",
        position_type="phd",
    )

    assert decision.status == "rejected"
    assert decision.reason == "explicitly_closed_or_unavailable"


def test_navigation_link_is_rejected():
    decision = screen_position("Read more", "https://example.test/about")
    assert decision.status == "rejected"
    assert decision.reason == "navigation_link"


@pytest.mark.parametrize(
    "title",
    [
        "Kontakt",
        "Stellenangebote",
        "Partners",
        "Contacts",
        "Open positions",
        "Vacancies",
        "Current vacancies",
        "View all vacancies",
        "Job vacancies",
        "Jobs by email",
        "AAU PhD Career Hub",
        "Academic vacancies",
        "Advanced job search",
        "Aktuelle Stellenangebote",
        "Current opportunities",
        "EURAXESS - European Job Portal",
        "Fellowships",
        "Internships",
        "Job offers",
        "Job opportunities",
        "Job portal of TU Darmstadt",
        "Jobs",
        "Jobs by RSS",
        "PhD opportunities",
        "PhD vacancies",
        "Postdoctoral opportunities",
        "Recruitment",
        "Research opportunities",
        "The recruitment and selection lifecycle",
        "Vacancies and how to apply",
        "Work with us",
        "Criteri di valutazione",
        "Modello di domanda",
        "Líneas y equipos de investigación",
        "Page not found",
        "FAQs",
    ],
)
def test_multilingual_category_titles_are_navigation_not_positions(title):
    decision = screen_position(title, "https://example.test/category")

    assert decision.status == "rejected"
    assert decision.reason == "navigation_link"


@pytest.mark.parametrize(
    "title",
    [
        "Duales Studium als Handelsmanager | (m/w/d)",
        "Duales Studium Financial Consultant | (m/w/d)",
        "Освітня програма",
    ],
)
def test_vocational_and_generic_degree_programmes_are_not_research_positions(title):
    decision = screen_position(title, "https://example.test/study")

    assert decision.status == "rejected"
    assert decision.reason == "degree_or_course_page"


def test_live_named_degree_programme_is_not_rejected_before_evidence_review():
    decision = screen_position(
        "Master's degree in Advanced Design",
        "https://example.test/study/advanced-design",
        "Advanced Design Master's programme. Applications for the 2027 intake are now open.",
    )

    assert decision.status == "review"
    assert decision.reason == "current_or_future_programme_intake"


def test_live_named_phd_programme_remains_reviewable_or_eligible():
    decision = screen_position(
        "PhD programme in Marine Engineering",
        "https://example.test/study/marine-engineering",
        "Applications for the 2027 intake are now open.",
    )

    assert decision.status != "rejected"


@pytest.mark.parametrize(
    ("title", "url", "description"),
    [
        ("Payment by transfer", "https://example.test/payment", "Transfer semester fees to the university bank account."),
        ("Making sure you're not a bot!", "https://example.test/challenge", "Stars and repository navigation"),
        ("Webinar Recording", "https://example.test/media/1", "Recorded seminar media page."),
        ("Guide: the PhD Journey in the Netherlands", "https://example.test/guide", "Doctoral study guide."),
        ("How to apply", "https://example.test/phd/how-to-apply", "PhD programme application instructions."),
        ("Information For", "https://example.test/alumni", "Athena Scholarship - apply now."),
        ("Scholarships", "https://example.test/scholarships", "Search all available awards."),
        ("Sport scholarships", "https://example.test/sport", "Applications for athletes are open."),
        ("IPA 2027EUR: Call for Papers", "https://example.test/conference", "Submission deadline 11 September."),
        ("Bandi", "https://example.test/calls#position-1", "Bandi"),
        ("Contratti di ricerca", "https://example.test/calls#position-2", "Contratti di ricerca"),
        ("PhD Brochure", "https://example.test/doctoral/brochure.pdf", "Programme information."),
        ("2022-07-08 - PhD Programme outline ENG - PDF", "https://example.test/outline.pdf", "Course outline."),
        ("How does it work?", "https://example.test/how", "General procedural information."),
    ],
)
def test_known_non_opportunity_pages_are_rejected(title, url, description):
    decision = screen_position(title, url, description)

    assert decision.status == "rejected"
    assert decision.reason == "non_opportunity_page"


@pytest.mark.parametrize(
    "title",
    [
        "Fees and funding",
        "Tuition fees, funding and scholarships",
        "Fees, funding and scholarships",
        "Fees and Scholarships",
        "Fees & Grants",
    ],
)
def test_generic_fees_and_funding_pages_are_not_opportunities(title):
    decision = screen_position(title, "https://example.test/study/fees-funding")

    assert decision.status == "rejected"
    assert decision.reason == "non_opportunity_page"


def test_fees_heading_with_a_concrete_current_call_remains_reviewable():
    decision = screen_position(
        "Fees & Grants",
        "https://example.test/funding/current-call",
        "A funded doctoral position is available. Applications are now open until 30 June 2027.",
    )

    assert decision.status != "rejected"


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Graduatoria finale dottorati \u2013 prot.8284/2024",
            "Bando di concorso per l'ammissione al corso di dottorato. "
            "Application deadline: 12 September 2024.",
        ),
        (
            "Resolución definitiva de contratos predoctorales 2025",
            "Finalizado el plazo para la presentación de solicitudes.",
        ),
        (
            "Resolución provisional de beneficiarios/as",
            "Applications are currently open for the original award call.",
        ),
    ],
)
def test_selection_outcomes_are_rejected_even_if_the_original_call_is_embedded(
    title, description
):
    decision = screen_position(
        title,
        "https://example.test/results/document.pdf",
        description,
        position_type="phd",
    )

    assert decision.status == "rejected"
    assert decision.reason == "selection_result_page"


def test_known_non_opportunity_title_is_not_protected_by_its_own_vacancy_word():
    decision = screen_position(
        "Administrative Vacancies",
        "https://example.test/administrative-vacancies",
        "Browse academic vacancies and employment opportunities from the navigation below.",
    )

    assert decision.status == "rejected"
    assert decision.reason == "non_opportunity_page"


def test_category_like_title_with_real_call_details_is_not_rejected():
    decision = screen_position(
        "Contratti di ricerca",
        "https://example.test/calls#position-2",
        "Applications are invited for a funded research position. Submit your CV by 30 June.",
    )

    assert decision.status == "review"
    assert decision.reason == "ambiguous_candidate"


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Information For",
            "Athena Scholarship \u2013 apply now until 31 Aug 26!",
        ),
        (
            "Sport scholarships",
            "Sport scholarships are available. Applications for the 2026/2027 academic year are now open.",
        ),
        (
            "Open positions",
            "A postdoctoral position is offered. The application deadline is 10 July 2027.",
        ),
    ],
)
def test_generic_heading_with_concrete_current_call_is_not_rejected(
    title, description
):
    decision = screen_position(
        title,
        "https://example.test/opportunities/current",
        description,
        position_type="research_fellowship",
    )

    assert decision.status != "rejected"


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Information For", "Athena Scholarship - apply now."),
        ("Sport scholarships", "Support for athletes and scholarship navigation."),
        ("Open positions", "Browse all departments and research areas."),
    ],
)
def test_generic_heading_without_concrete_application_window_stays_rejected(
    title, description
):
    decision = screen_position(
        title,
        "https://example.test/category",
        description,
    )

    assert decision.status == "rejected"


@pytest.mark.parametrize(
    "title",
    [
        "Procedure di selezione pubblica per il reclutamento di n. 7 ricercatori RTT",
        "Borse di studio Fondazione Lilli",
    ],
)
def test_concrete_calls_are_not_mistaken_for_generic_category_fragments(title):
    decision = screen_position(
        title,
        "https://example.test/calls#position-deadbeefdeadbeef",
        title,
    )

    assert decision.status != "rejected"


@pytest.mark.parametrize(
    "title",
    [
        "CONCORSO NAZIONALE A PREMI PER TESI DI LAUREA E DI DOTTORATO 2025",
        "Best PhD Thesis Award 2026",
    ],
)
def test_thesis_prizes_are_not_published_as_research_positions(title):
    decision = screen_position(
        title,
        "https://example.test/doctoral-awards",
        "Submit a completed thesis to win a monetary prize.",
        position_type="phd",
    )

    assert decision == ScreeningDecision("rejected", "non_opportunity_page")


def test_recruiting_thesis_position_is_not_mistaken_for_a_thesis_prize():
    decision = screen_position(
        "Fully funded Master's thesis position",
        "https://example.test/jobs/thesis-position",
        "Applications are open until 31 December 2026.",
        position_type="masters_mph",
    )

    assert decision.status != "rejected"


def test_administrative_doctoral_pages_are_rejected_without_vacancy_signals():
    cases = (
        ("enrollment", "PhD students complete enrollment and pay fees for thesis preparation."),
        ("Change of Director of thesis", "The PhD student submits a form to change the thesis director."),
        ("Extension of thesis reading deadline", "The thesis defence deadline is extended by the academic committee."),
        (
            "Interruption/leave and readmission to doctoral studies",
            "The doctoral student submits the readmission form to the academic committee.",
        ),
        (
            "application for admission to thesis defense and proposal of Tribunal",
            "The PhD student submits a thesis defence form and proposal of tribunal.",
        ),
        ("committee of Ethics (CEI)", "Projects must be assessed and approved through the ethical assessment form."),
        ("Co-authored thesis", "The title of Doctor will include the co-supervision agreement for the thesis."),
        (
            "International Doctorate",
            "Once the thesis has been defended, the accredited specialization may be requested.",
        ),
        (
            "Regulations of the University of Navarra",
            "Internal regulations, code of good practices and protocol for doctoral theses.",
        ),
        (
            "procedure",
            "Extraordinary prizes are awarded after examination of the thesis by a tribunal and recorded in the academic certificate.",
        ),
    )
    for title, description in cases:
        decision = screen_position(
            title,
            "https://example.test/doctoral/information",
            description,
        )
        assert decision.status == "rejected", title
        assert decision.reason == "administrative_non_vacancy_page"


@pytest.mark.parametrize(
    "title",
    [
        "Application and admission process",
        "Access routes",
        "Industrial Doctorate",
        "Conditions for granting",
        "procedure",
        "enrollment",
    ],
)
def test_ambiguous_administrative_titles_are_not_rejected_without_corroboration(title):
    decision = screen_position(title, "https://example.test/doctoral/information")

    assert decision.status == "review"


@pytest.mark.parametrize(
    "title",
    [
        "Selection procedure for recruitment of one Associate Professor",
        "Student Administration Senior Manager (Enrolment and Records)",
        "PhD position in transcription regulation",
    ],
)
def test_recruitment_titles_are_never_treated_as_administrative(title):
    decision = screen_position(
        title,
        "https://example.test/jobs/1",
        "Applications are invited. Submit your CV for this employment contract.",
    )

    assert decision.status == "eligible"


def test_vacancy_signal_protects_similarly_named_administrative_pages():
    decision = screen_position(
        "PhD position: enrollment and admission process",
        "https://example.test/jobs/phd-enrollment",
    )

    assert decision.status == "eligible"
    assert decision.reason == "vacancy_signal"


@pytest.mark.parametrize(
    ("title", "url"),
    [
        ("Admission procedure", "https://example.test/Topics_PhD_2026.pdf"),
        ("Application Process", "https://example.test/doctoral-programmes/"),
        ("Application Procedure", "https://example.test/honorary-research-fellowship"),
    ],
)
def test_generic_application_titles_remain_reviewable_without_more_context(title, url):
    decision = screen_position(title, url)

    assert decision.status == "review"
    assert decision.reason == "ambiguous_candidate"


def test_enriched_guard_rejects_only_explicit_non_negated_closure():
    closed = screen_enriched_position(
        "Predoctoral position in AI",
        "https://example.test/jobs/1",
        "Applications are now closed. The successful candidate would join the lab.",
        "phd",
    )
    open_call = screen_enriched_position(
        "Predoctoral position in AI",
        "https://example.test/jobs/1",
        "Applications are not closed and remain open until 31 December.",
        "phd",
    )

    assert closed.status == "rejected"
    assert closed.reason == "detail_explicitly_closed"
    assert detail_rejection_evidence("Application status: CLOSED") == "Application status: CLOSED"
    assert open_call.status == "eligible"
    assert detail_rejection_evidence("Applications are not closed") is None
    assert (
        detail_rejection_evidence("The SSCP DTP no longer accepts applications")
        == "The SSCP DTP no longer accepts applications"
    )


def test_spanish_expired_application_window_is_explicit_and_negation_is_safe():
    expired = (
        "Finalizado el plazo para la presentación de solicitudes de becas, "
        "se publica la resolución definitiva."
    )
    still_open = "No ha finalizado el plazo para la presentación de solicitudes."

    assert detail_rejection_evidence(expired) == expired
    assert detail_rejection_evidence(still_open) is None


def test_enriched_guard_does_not_reject_a_recruiting_degree_like_title():
    decision = screen_enriched_position(
        "Master in artificial intelligence",
        "https://example.test/opportunity/1",
        "Applications are invited for a funded Master's thesis position. Submit your application by 31 December.",
        "masters_mph",
    )

    assert decision.status == "review"
    assert decision.reason == "recognized_type_without_vacancy_signal:masters_mph"
