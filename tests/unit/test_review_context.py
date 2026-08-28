from datetime import date

import pytest

from phd_searcher.pipeline.review_context import (
    application_evidence_supports,
    build_evidence_context,
    classify_opportunity_kind_evidence,
    evidence_quote_present,
    explicit_negative_evidence_supports,
    has_future_deadline_status_conflict,
    negative_evidence_supports,
    opportunity_kind_evidence_supports,
    select_evidence_document,
    triage_evidence_supports,
)


def test_evidence_document_falls_back_only_for_known_fetch_errors():
    inline = "Candidates can join advanced research projects in data engineering."

    assert select_evidence_document(inline, '``` {"error_type": "LanguageFolder"} ```') == inline
    assert select_evidence_document(inline, "A short but valid fetched page") == "A short but valid fetched page"
    assert select_evidence_document(inline, None) == inline


def test_euraxess_job_scope_removes_site_footer_but_preserves_attributable_status():
    title = "PhD Position eXtended Reality for Inclusive Automated Vehicle Interaction"
    fetched = (
        f"site navigation {title} 18 Jul 2026 "
        "## Job Information Organisation Delft University of Technology "
        "Application Deadline 30 Aug 2026 - 21:59 (UTC) Country Netherlands "
        "## Offer Description Applications are invited for this PhD position in "
        "Virtual Reality and eXtended Reality. "
        "## Work Location(s) Delft ## Contact City Delft "
        "STATUS: EXPIRED [Apply now](https://academictransfer.example/apply/) "
        "##### Share this page unrelated navigation STATUS: OPEN"
    )

    selected = select_evidence_document(
        "Virtual Reality research position.",
        fetched,
        title=title,
        url="https://euraxess.ec.europa.eu/jobs/453992",
        deadline=date(2026, 8, 30),
        today=date(2026, 8, 26),
    )

    assert selected.startswith(title)
    assert "Application Deadline 30 Aug 2026" in selected
    assert "Virtual Reality and eXtended Reality" in selected
    assert "STATUS: EXPIRED" in selected
    assert "unrelated navigation" not in selected
    assert has_future_deadline_status_conflict(
        "Virtual Reality research position.",
        fetched,
        title=title,
        url="https://euraxess.ec.europa.eu/jobs/453992",
        deadline=date(2026, 8, 30),
        today=date(2026, 8, 26),
    )


def test_euraxess_candidate_scope_never_hides_attributable_or_elapsed_closure():
    title = "PhD position in interaction design"
    body_closed = (
        f"navigation {title} ## Job Information "
        "Application Deadline 30 Aug 2026 - 21:59 (UTC) "
        "## Offer Description Application status: CLOSED. "
        "## Work Location Delft ## Contact Example"
    )
    elapsed_footer_closed = (
        f"navigation {title} ## Job Information "
        "Application Deadline 20 Aug 2026 - 21:59 (UTC) "
        "## Offer Description A funded doctoral position. "
        "## Work Location Delft ## Contact Example STATUS: EXPIRED"
    )

    selected_body = select_evidence_document(
        "Interaction design research.",
        body_closed,
        title=title,
        url="https://euraxess.ec.europa.eu/jobs/1",
        deadline=date(2026, 8, 30),
        today=date(2026, 8, 26),
    )
    selected_elapsed = select_evidence_document(
        "Interaction design research.",
        elapsed_footer_closed,
        title=title,
        url="https://euraxess.ec.europa.eu/jobs/2",
        deadline=date(2026, 8, 20),
        today=date(2026, 8, 26),
    )

    assert "Application status: CLOSED" in selected_body
    assert "STATUS: EXPIRED" in selected_elapsed
    assert not has_future_deadline_status_conflict(
        "Interaction design research.",
        body_closed,
        title=title,
        url="https://euraxess.ec.europa.eu/jobs/1",
        deadline=date(2026, 8, 30),
        today=date(2026, 8, 26),
    )


def test_evidence_context_keeps_head_deadline_window_and_tail():
    text = "HEAD " + ("background " * 700) + "Application deadline 31 December 2030. " + ("body " * 700) + "TAIL"
    context = build_evidence_context(text, max_chars=3000)

    assert context.startswith("HEAD")
    assert "Application deadline 31 December 2030" in context
    assert context.endswith("TAIL")
    assert len(context) <= 3000


def test_evidence_context_prioritizes_late_project_application_actions():
    text = (
        "PROJECT HEAD "
        + ("application information and programme background " * 220)
        + ("research details " * 180)
        + "How to Apply: Apply using our online Postgraduate Applications Portal. Apply now. "
        + ("footer " * 220)
    )

    context = build_evidence_context(text, max_chars=3_000)

    assert context.startswith("PROJECT HEAD")
    assert "How to Apply: Apply using our online Postgraduate Applications Portal. Apply now" in context
    assert context.endswith("footer")
    assert len(context) <= 3_000


def test_evidence_quote_validation_ignores_whitespace_and_case():
    context = "Applications   ARE invited for one doctoral position."
    assert evidence_quote_present("applications are invited", context)
    assert not evidence_quote_present("deadline tomorrow", context)
    assert not evidence_quote_present("position", context)


def test_evidence_quote_validation_ignores_only_pdf_spacing_inside_brackets():
    context = (
        "How to apply? Please send your application to Dr. Schlesiger "
        "( m.schlesiger@example.org)."
    )
    assert evidence_quote_present(
        "How to apply? Please send your application to Dr. Schlesiger "
        "(m.schlesiger@example.org)",
        context,
    )
    assert not evidence_quote_present(
        "Please do not send your application to Dr. Schlesiger "
        "(m.schlesiger@example.org)",
        context,
    )


def test_evidence_quote_validation_ignores_markdown_presentation_only():
    context = (
        "The call is **SCADUTO**. See "
        "[currently available Master thesis projects](https://example.test/projects)."
    )

    assert evidence_quote_present("The call is SCADUTO", context)
    assert evidence_quote_present("currently available Master thesis projects", context)
    assert evidence_quote_present('\"currently available Master thesis projects\"', context)
    assert not evidence_quote_present("no currently available Master thesis projects", context)
    assert not evidence_quote_present("Master thesis projects currently available", context)


def test_evidence_quote_validation_ignores_single_markdown_emphasis_only():
    context = (
        "Whilst the SSCP DTP *no longer accepts applications*, any PhD "
        "opportunities will be advertised here."
    )

    assert evidence_quote_present(
        "Whilst the SSCP DTP _no longer accepts applications_, any PhD "
        "opportunities will be advertised here",
        context,
    )
    assert not evidence_quote_present(
        "Whilst the SSCP DTP _now accepts applications_, any PhD "
        "opportunities will be advertised here",
        context,
    )


def test_evidence_quote_validation_ignores_invisible_layout_and_terminal_punctuation():
    context = (
        "Applications from non-\u200bpermanent researchers close on "
        "**31 August 2026**, at noon."
    )

    assert evidence_quote_present(
        "Applications from non-permanent researchers close on 31 August 2026.",
        context,
    )
    assert not evidence_quote_present(
        "Applications from permanent researchers close on 31 August 2026.",
        context,
    )


def test_evidence_facts_require_semantically_relevant_quotes():
    assert application_evidence_supports(
        ["Applications are invited", "Funded doctoral position"],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
    )
    assert not application_evidence_supports(
        ["The university was founded in 1901"],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
    )
    assert negative_evidence_supports(
        ["Applications are now closed"],
        actual_vacancy="unknown",
        open_status="closed",
    )
    assert not negative_evidence_supports(
        ["The university was founded in 1901"],
        actual_vacancy="unknown",
        open_status="closed",
    )


def test_null_deadline_field_is_not_an_open_application_signal():
    vacancy = "School of Psychology | PHD"
    null_deadline = "Application Deadline** None specified **Start Date** 21 September 2026"

    assert not application_evidence_supports(
        [vacancy, null_deadline],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
    )
    assert application_evidence_supports(
        [vacancy, null_deadline, "How to apply: Apply now using our online portal"],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
    )


@pytest.mark.parametrize(
    "relative_window",
    [
        "Applications must be submitted within 30 days from publication",
        "Le candidature devono essere presentate entro 30 giorni dalla pubblicazione",
        "Bewerbungsfrist: innerhalb von 30 Tagen nach der Veröffentlichung",
        "Date limite de candidature : dans un délai de 30 jours à compter de la publication",
        "Plazo de presentación de solicitudes: dentro de los 30 días siguientes a la publicación",
    ],
)
def test_relative_publication_window_without_anchor_cannot_prove_open_status(relative_window):
    assert not application_evidence_supports(
        ["Funded doctoral position", relative_window],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
    )


@pytest.mark.parametrize(
    "anchor",
    [
        "Apply now using our online application portal",
        "Applications are currently open",
        "Application deadline: 31 December 2030",
    ],
)
def test_relative_publication_window_accepts_current_or_absolute_anchor(anchor):
    assert application_evidence_supports(
        [
            "Funded doctoral position",
            "Applications must be submitted within 30 days from publication",
            anchor,
        ],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
        today=date(2026, 8, 9),
    )


def test_relative_publication_window_ignores_an_unrelated_absolute_date():
    assert not application_evidence_supports(
        [
            "Funded doctoral position",
            "Applications must be submitted within 30 days from publication",
            "The research project starts on 31 December 2030",
        ],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
        today=date(2026, 8, 9),
    )


def test_relative_publication_window_requires_a_live_temporal_anchor():
    vacancy = "Funded doctoral position"
    relative_window = "Applications must be submitted within 30 days from publication"

    assert not application_evidence_supports(
        [vacancy, relative_window, "Published on 1 January 2022"],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
        today=date(2026, 8, 9),
    )
    assert application_evidence_supports(
        [vacancy, relative_window, "Published on 1 August 2026"],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
        today=date(2026, 8, 9),
    )
    assert application_evidence_supports(
        [vacancy, relative_window, "Application deadline: 31 August 2026"],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
        today=date(2026, 8, 9),
    )


def test_grants_and_research_associate_roles_are_concrete_opportunities():
    assert application_evidence_supports(
        [
            "Convocatoria de ayudas para participación en congresos",
            "Plazo de presentación de solicitudes: 31 diciembre 2030",
        ],
        actual_vacancy="yes",
        open_status="open",
        position_type="research_fellowship",
    )
    assert application_evidence_supports(
        ["Applications are invited for a Research Associate position"],
        actual_vacancy="yes",
        open_status="open",
        position_type="research_staff",
    )


def test_open_call_requires_current_or_unexpired_timing_not_only_a_title():
    assert not application_evidence_supports(
        ["Convocatoria de ayudas para participación en Congresos"],
        actual_vacancy="yes",
        open_status="open",
        position_type="research_fellowship",
        today=date(2026, 8, 18),
    )
    assert not application_evidence_supports(
        [
            "Convocatoria de ayudas para participación en Congresos",
            "Los estudiantes podrán solicitar esta ayuda entre el 15 y el 30 de junio de 202 6",
        ],
        actual_vacancy="yes",
        open_status="open",
        position_type="research_fellowship",
        today=date(2026, 8, 18),
    )
    assert application_evidence_supports(
        [
            "Convocatoria de ayudas para participación en Congresos",
            "Los estudiantes podrán solicitar esta ayuda del 24 de julio al 4 de septiembre de 202 6",
        ],
        actual_vacancy="yes",
        open_status="open",
        position_type="research_fellowship",
        today=date(2026, 8, 18),
    )


def test_multilingual_awards_studentships_and_teaching_calls_are_supported():
    assert application_evidence_supports(
        ["If you would like to apply for a studentship at Kingston University"],
        actual_vacancy="yes",
        open_status="closed",
        position_type="research_fellowship",
    )
    assert application_evidence_supports(
        [
            "Primera Edición de los Premios Madrid Accesible",
            "Inscripciones están abiertas desde el martes 14 de julio de 2026",
        ],
        actual_vacancy="yes",
        open_status="open",
        position_type="research_fellowship",
    )
    assert application_evidence_supports(
        [
            "ETH Career Seed Awards provide a funding opportunity for postdocs",
            "Submission deadlines are 1 March and 1 September each year",
        ],
        actual_vacancy="yes",
        open_status="future",
        position_type="research_fellowship",
    )
    assert application_evidence_supports(
        [
            "Avviso di selezioni pubbliche per il conferimento di contratti integrativi "
            "di insegnamenti ufficiali"
        ],
        actual_vacancy="yes",
        open_status="closed",
        position_type="faculty",
    )
    assert application_evidence_supports(
        ["Course type: Dottorato", "Positions: 8"],
        actual_vacancy="yes",
        open_status="unknown",
        position_type="phd",
    )
    assert application_evidence_supports(
        ["ETH Zurich - IDEA League Mobility Grants", "Requests can be submitted anytime"],
        actual_vacancy="yes",
        open_status="open",
        position_type="research_fellowship",
    )


def test_generic_programme_wording_cannot_alone_support_a_non_vacancy_rejection():
    for generic_page in (
        "Programa de Doctorado en Ciencias",
        "News and events",
        "The programme began on 31 December 2020",
    ):
        assert not negative_evidence_supports(
            [generic_page],
            actual_vacancy="no",
            open_status="unknown",
        )
        assert not negative_evidence_supports(
            [generic_page],
            actual_vacancy="unknown",
            open_status="closed",
        )


def test_opportunity_kind_separates_vacancies_programmes_spontaneous_and_information():
    generic = ["Information for prospective graduate students", "How to apply using the portal"]
    programme = [
        "PhD@Work Programme",
        "Applications for the 2027 intake are now open",
    ]
    spontaneous = [
        "Please send your application directly to the head of the research group",
    ]
    vacancy = [
        "Selection process for one fixed-term Tenure Track Researcher position",
        "Application deadline: 31 December 2030",
    ]

    assert not opportunity_kind_evidence_supports(generic, "vacancy")
    assert opportunity_kind_evidence_supports(generic, "information")
    assert opportunity_kind_evidence_supports(programme, "programme")
    assert not opportunity_kind_evidence_supports(programme, "vacancy")
    assert opportunity_kind_evidence_supports(spontaneous, "spontaneous")
    assert not opportunity_kind_evidence_supports(spontaneous, "vacancy")
    assert opportunity_kind_evidence_supports(vacancy, "vacancy")
    assert not opportunity_kind_evidence_supports(vacancy, "unknown")
    assert classify_opportunity_kind_evidence(spontaneous) == "spontaneous"
    assert classify_opportunity_kind_evidence(programme) == "programme"
    assert classify_opportunity_kind_evidence(vacancy) == "vacancy"
    assert classify_opportunity_kind_evidence(generic) == "information"
    assert classify_opportunity_kind_evidence(["University overview"]) == "unknown"


def test_programme_kind_requires_a_current_or_future_intake_not_only_instructions():
    guide = [
        "PhD@Work Programme",
        "Information about how to apply is available on the programme home page",
    ]
    rolling = [
        "Heidelberg Biosciences International Graduate School PhD programme",
        "Qualified candidates may submit applications throughout the whole year",
    ]
    stale = [
        "Example University PhD programme",
        "Applications for the 2022 intake",
    ]
    current = [
        "Example University PhD programme",
        "Applications for the 2026/27 intake",
    ]
    future = [
        "Example University PhD programme",
        "Applications for the 2027 intake",
    ]
    unexpired = [
        "Example University PhD programme",
        "Application deadline: 31 December 2026",
    ]
    expired = [
        "Example University PhD programme",
        "Application deadline: 15 July 2026",
    ]

    current_date = date(2026, 8, 9)
    assert not opportunity_kind_evidence_supports(guide, "programme", today=current_date)
    assert opportunity_kind_evidence_supports(rolling, "programme", today=current_date)
    assert not opportunity_kind_evidence_supports(stale, "programme", today=current_date)
    assert opportunity_kind_evidence_supports(current, "programme", today=current_date)
    assert opportunity_kind_evidence_supports(future, "programme", today=current_date)
    assert opportunity_kind_evidence_supports(unexpired, "programme", today=current_date)
    assert not opportunity_kind_evidence_supports(expired, "programme", today=current_date)


@pytest.mark.parametrize(
    "recurring_deadline",
    [
        "Application Deadline: Any time during the year",
        "Application Deadline: Once a year from mid November until mid January",
        "Application Deadline: March 1st and October 1st",
    ],
)
def test_programme_kind_accepts_explicit_recurring_deadlines(recurring_deadline):
    programme = [
        "Bonn International Graduate School PhD programme",
        recurring_deadline,
    ]

    assert opportunity_kind_evidence_supports(
        programme,
        "programme",
        today=date(2026, 8, 9),
    )
    assert classify_opportunity_kind_evidence(
        programme,
        today=date(2026, 8, 9),
    ) == "programme"


def test_deadline_alone_does_not_turn_a_programme_into_a_vacancy():
    programme = [
        "Example University PhD programme",
        "Application deadline: 31 December 2026",
    ]

    assert opportunity_kind_evidence_supports(
        programme,
        "programme",
        today=date(2026, 8, 9),
    )
    assert not opportunity_kind_evidence_supports(
        programme,
        "vacancy",
        today=date(2026, 8, 9),
    )


def test_specific_phd_project_and_localized_call_are_vacancy_kinds():
    assert opportunity_kind_evidence_supports(
        [
            "AI Enabled Supply Chain Resilience in Food Manufacturing",
            "School of Biological Sciences | PHD Funding Funded Reference Number SBIO-2024-004",
            "Application Deadline 31 August 2026",
        ],
        "vacancy",
        today=date(2026, 8, 18),
    )
    assert opportunity_kind_evidence_supports(
        [
            "Avviso pubblico di selezione per l'affidamento di n. 1 incarico di lavoro autonomo",
            "Scadenza: 27 agosto 2026",
        ],
        "vacancy",
        today=date(2026, 8, 18),
    )


def test_named_research_structures_with_recurring_intake_are_programmes():
    assert opportunity_kind_evidence_supports(
        [
            "Cluster of Excellence: ECONtribute: Markets & Public Policy",
            "Application Deadline: Any time during the year",
        ],
        "programme",
        today=date(2026, 8, 18),
    )


def test_past_application_invitation_cannot_prove_current_openness():
    historical = ["Applications were invited for a funded doctoral position"]

    assert not application_evidence_supports(
        historical,
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
        today=date(2026, 8, 9),
    )
    assert application_evidence_supports(
        [*historical, "Applications are currently open"],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
        today=date(2026, 8, 9),
    )
    assert application_evidence_supports(
        [*historical, "Application deadline: 31 December 2026"],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
        today=date(2026, 8, 9),
    )


def test_negated_open_signal_cannot_prove_current_openness():
    assert not application_evidence_supports(
        ["Funded doctoral position", "Applications are not currently open"],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
        today=date(2026, 8, 9),
    )
    assert not application_evidence_supports(
        ["Funded doctoral position", "This call is not open for applications"],
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
        today=date(2026, 8, 9),
    )


def test_past_application_deadline_supports_closed_but_future_opening_is_actionable():
    assert negative_evidence_supports(
        ["Application deadline: 15 July 2026"],
        actual_vacancy="yes",
        open_status="closed",
        today=date(2026, 8, 9),
    )
    assert negative_evidence_supports(
        [
            "Los estudiantes interesados podrán solicitar esta ayuda, "
            "entre el 1 y el 15 de julio de 2026"
        ],
        actual_vacancy="yes",
        open_status="closed",
        today=date(2026, 8, 9),
    )
    assert not negative_evidence_supports(
        ["The conference took place on 15 July 2026"],
        actual_vacancy="yes",
        open_status="closed",
        today=date(2026, 8, 9),
    )
    spanning_window = "Applications can be submitted between 1 July 2026 and 31 December 2026"
    assert not negative_evidence_supports(
        [spanning_window],
        actual_vacancy="yes",
        open_status="closed",
        today=date(2026, 8, 9),
    )
    assert negative_evidence_supports(
        [spanning_window],
        actual_vacancy="yes",
        open_status="closed",
        today=date(2027, 1, 1),
    )
    assert application_evidence_supports(
        [
            "Applications for the 2027\u20132028 cycle will open in November 2026",
            "Early-Career Fellowships",
        ],
        actual_vacancy="yes",
        open_status="future",
        position_type="research_fellowship",
    )


def test_teaching_call_language_supports_a_faculty_opportunity():
    assert application_evidence_supports(
        [
            "Avviso di interesse per carichi di didattica",
            "Scadenza della domanda: 31 dicembre 2030",
        ],
        actual_vacancy="yes",
        open_status="open",
        position_type="faculty",
    )


def test_german_student_helper_evidence_supports_an_open_assistantship():
    evidence = [
        "stud. Hilfskraft (m/w/d)(5h/Woche)",
        "Bewerbung senden Sie bitte bis zum 31.08.2030",
    ]

    assert opportunity_kind_evidence_supports(evidence, "vacancy")
    assert application_evidence_supports(
        evidence,
        actual_vacancy="yes",
        open_status="open",
        position_type="assistantship",
    )


def test_named_intern_role_and_apply_now_support_an_open_internship():
    evidence = ["Trade Marketing Intern", "Apply Now!"]

    assert opportunity_kind_evidence_supports(evidence, "vacancy")
    assert application_evidence_supports(
        evidence,
        actual_vacancy="yes",
        open_status="open",
        position_type="internship",
    )


def test_open_till_clause_supports_an_elapsed_deadline():
    evidence = ["The vacancy is open till the 15th of June 2022"]

    assert negative_evidence_supports(
        evidence,
        actual_vacancy="yes",
        open_status="closed",
        today=date(2026, 8, 18),
    )


def test_closed_evidence_does_not_invert_explicit_negation():
    assert not negative_evidence_supports(
        ["Applications are not closed"],
        actual_vacancy="unknown",
        open_status="closed",
    )
    assert not explicit_negative_evidence_supports(
        ["Application status: not closed"],
        actual_vacancy="unknown",
        open_status="closed",
    )
    assert explicit_negative_evidence_supports(
        ["Status: CLOSED"],
        actual_vacancy="unknown",
        open_status="closed",
    )
    assert explicit_negative_evidence_supports(
        ["There are no open positions"],
        actual_vacancy="no",
        open_status="unknown",
    )
    assert negative_evidence_supports(
        ["The SSCP DTP no longer accepts applications"],
        actual_vacancy="unknown",
        open_status="closed",
    )
    assert explicit_negative_evidence_supports(
        ["The SSCP DTP no longer accepts applications"],
        actual_vacancy="unknown",
        open_status="closed",
    )


def test_fast_triage_evidence_requires_application_or_negative_signals():
    assert triage_evidence_supports(
        ["Applications are invited for a funded doctoral position"],
        decision="eligible",
        position_type="phd",
    )
    assert not triage_evidence_supports(
        ["The university has a doctoral school"],
        decision="eligible",
        position_type="phd",
    )
    assert triage_evidence_supports(
        ["Applications are now closed"],
        decision="rejected",
        position_type="other",
    )
