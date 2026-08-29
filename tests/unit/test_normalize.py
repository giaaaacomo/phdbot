from datetime import date

from phd_searcher.pipeline.normalize import (
    extract_deadline,
    extract_research_group,
    extract_terms,
    normalize_item,
    parse_compensation,
    parse_dates,
    parse_deadline,
)


def test_parse_deadline_iso_embedded():
    assert parse_deadline("Deadline: 2026-09-01 23:59") == date(2026, 9, 1)


def test_parse_deadline_european_format():
    assert parse_deadline("01/09/2026") == date(2026, 9, 1)


def test_parse_deadline_euraxess_label_and_timezone():
    assert parse_deadline("Application Deadline:15 Aug 2026 - 00:00 (UTC)") == date(2026, 8, 15)


def test_parse_deadline_embedded_numeric_date():
    assert parse_deadline("Apply no later than 31.07.2026 at 23:59") == date(2026, 7, 31)


def test_parse_deadline_unparseable_is_none():
    assert parse_deadline("rolling admission") is None
    assert parse_deadline(None) is None


def test_null_application_deadline_does_not_borrow_the_start_date():
    contaminated = "Application Deadline** None specified **Start Date** 21 September 2026"

    assert parse_deadline(contaminated) is None
    assert extract_deadline(contaminated) == (None, None)

    normalized = normalize_item(
        {"title": "PhD project", "url": "/project", "deadline": contaminated},
        base_url="https://uni.example/",
    )
    assert normalized is not None
    assert normalized.deadline is None
    assert normalized.deadline_raw == contaminated


def test_parse_deadline_supports_localized_months_and_range_end():
    assert parse_deadline("entre el 1 y el 15 de julio de 2026") == date(2026, 7, 15)
    assert parse_deadline("Scadenza: 31 dicembre 2030") == date(2030, 12, 31)
    assert parse_deadline("Date limite: 3 février 2027") == date(2027, 2, 3)
    assert parse_dates("from 1 July 2026 to 15 August 2026") == [
        date(2026, 7, 1),
        date(2026, 8, 15),
    ]


def test_parse_dates_repairs_year_digits_split_by_pdf_ocr():
    assert parse_dates("dal 24 luglio 2026 al 4 settembre 202 6") == [
        date(2026, 7, 24),
        date(2026, 9, 4),
    ]
    assert parse_dates("30 de junio de 2 0 2 6") == [date(2026, 6, 30)]


def test_parse_dates_supports_spanish_del_before_year():
    assert parse_dates("desde el 23 al 30 de septiembre del 2026") == [
        date(2026, 9, 30)
    ]
    assert parse_deadline("desde el 23 al 30 de septiembre del 2026") == date(
        2026, 9, 30
    )


def test_extract_deadline_requires_an_application_context():
    raw, parsed = extract_deadline(
        "Formalización de solicitudes y plazo de presentación. Los estudiantes podrán "
        "solicitar esta ayuda entre el 1 y el 15 de julio de 2026."
    )
    assert parsed == date(2026, 7, 15)
    assert raw is not None
    assert "plazo de presentación" in raw

    assert extract_deadline("The conference took place on 15 July 2026.") == (None, None)


def test_extract_deadline_repairs_ocr_year_and_ignores_later_interview_date():
    raw, parsed = extract_deadline(
        "Formalización de solicitudes y plazo de presentación. Los estudiantes "
        "podrán solicitar esta ayuda entre el 15 y el 30 de junio de 202 6."
    )
    assert parsed == date(2026, 6, 30)
    assert raw is not None

    raw, parsed = extract_deadline(
        "The application deadline is Sept 1, 2026 (5 pm, Swiss time). "
        "After the application deadline candidates will be informed. "
        "The interview will take place on October 29, 2026."
    )
    assert parsed == date(2026, 9, 1)
    assert raw is not None


def test_extract_deadline_supports_open_till_and_german_bis_clauses():
    raw, parsed = extract_deadline(
        "The vacancy is open till the 15th of June 2022, but early applications are encouraged."
    )
    assert parsed == date(2022, 6, 15)
    assert raw is not None

    raw, parsed = extract_deadline(
        "Bewerbung: Ihre aussagekräftige Bewerbung senden Sie bitte bis zum 31.08.2026."
    )
    assert parsed == date(2026, 8, 31)
    assert raw is not None


def test_normalize_joins_relative_url():
    item = {"title": "PhD in ML", "url": "/jobs/123", "deadline": "2026-10-01"}
    n = normalize_item(item, base_url="https://uni.example/vacancies")
    assert n is not None
    assert n.url == "https://uni.example/jobs/123"
    assert n.deadline == date(2026, 10, 1)


def test_normalize_drops_items_without_title():
    assert normalize_item({"url": "/x"}, base_url="https://uni.example/") is None


def test_normalize_uses_stable_listing_fragment_without_href():
    n = normalize_item({"title": "PhD in Design"}, base_url="https://uni.example/vacancies")
    assert n is not None
    assert n.url.startswith("https://uni.example/vacancies#position-")


def test_normalize_rejects_navigation_homepage_link_as_position_url():
    n = normalize_item(
        {"title": "MBA Associate Program Director", "url": "https://uni.example/"},
        base_url="https://uni.example/vacancies",
    )
    assert n is not None
    assert n.url.startswith("https://uni.example/vacancies#position-")


def test_normalize_rejects_localized_homepage_link_as_position_url():
    n = normalize_item(
        {"title": "Academic vacancy", "url": "https://uni.example/en/"},
        base_url="https://uni.example/en/academic-vacancies/",
    )
    assert n is not None
    assert n.url.startswith("https://uni.example/en/academic-vacancies/#position-")


def test_normalize_truncates_long_deadline_raw():
    n = normalize_item({"title": "PhD", "url": "/x", "deadline": "x" * 500}, base_url="https://uni.example/")
    assert n is not None
    assert n.deadline_raw is not None
    assert len(n.deadline_raw) == 256


def test_parse_compensation_range_currency_and_period():
    assert parse_compensation("€ 2.500 - 3.000 per month gross") == (2500.0, 3000.0, "EUR", "month")


def test_parse_compensation_supports_native_ecb_currency_codes():
    assert parse_compensation("Student scholarship: 2,000 PLN gross per month") == (
        2000.0,
        2000.0,
        "PLN",
        "month",
    )


def test_normalize_rejects_description_spillover_in_compensation():
    position = normalize_item(
        {
            "title": "PhD project",
            "url": "/project",
            "compensation": (
                "Our PhD community organizes social events. There is a range of sources "
                "of funding available for students and projects are advertised online."
            ),
        },
        base_url="https://uni.example/",
    )

    assert position is not None
    assert position.compensation_raw is None
    assert position.compensation_period is None


def test_normalize_keeps_descriptive_compensation_without_an_amount():
    position = normalize_item(
        {"title": "Researcher", "url": "/job", "compensation": "Competitive salary and benefits package"},
        base_url="https://uni.example/",
    )

    assert position is not None
    assert position.compensation_raw == "Competitive salary and benefits package"


def test_normalize_optional_duration_and_compensation():
    n = normalize_item(
        {
            "title": "PhD",
            "url": "/x",
            "duration": "36 months",
            "compensation": "CHF 52,000 per year",
        },
        base_url="https://uni.example/",
    )
    assert n is not None
    assert n.duration_raw == "36 months"
    assert n.compensation_max == 52000.0
    assert n.compensation_currency == "CHF"
    assert n.compensation_period == "year"


def test_extract_terms_from_full_detail_text():
    compensation, duration, published = extract_terms(
        "Posted on: 10 July 2026\nDuration: 36 months\nGross salary: 52,000 CHF per year"
    )
    assert compensation == "Gross salary: 52,000 CHF per year"
    assert duration == "Duration: 36 months"
    assert published == "Posted on: 10 July 2026"
    assert extract_research_group("Research group: Human-Centred Design Lab") == "Human-Centred Design Lab"


def test_extract_terms_recovers_a_permanent_contract_across_markdown_lines():
    _, duration, _ = extract_terms("Contract\n\nPermanent\n\nHours\n\n37")

    assert duration == "Contract: Permanent"


def test_extract_terms_joins_a_salary_label_to_its_markdown_value():
    compensation, _, _ = extract_terms(
        "Salary\n\n£60,484 - £73,058 per annum\nContract\nPermanent"
    )

    assert compensation == "Salary: £60,484 - £73,058 per annum"
