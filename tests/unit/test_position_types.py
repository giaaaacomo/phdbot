from phd_searcher.position_types import classify_position


def test_program_director_is_faculty_not_master_program():
    assert (
        classify_position(
            "MBA Associate Program Director",
            "The role oversees an accredited Master of Business Administration program.",
        )
        == "faculty"
    )


def test_description_can_classify_when_title_is_generic():
    assert classify_position("Open position", "This is a doctoral research opportunity.") == "phd"


def test_predoctoral_offers_are_phd_positions():
    assert classify_position("Pre-doctoral place offer in artificial intelligence") == "phd"
    assert classify_position("Predoctoral researcher in marine engineering") == "phd"


def test_open_faculty_positions_are_not_classified_from_unrelated_page_text():
    assert classify_position("Other Open Faculty Positions", "The page also mentions PhD programs.") == "faculty"


def test_internships_and_traineeships_are_first_class_position_types():
    assert classify_position("Research Internship in Marine Engineering") == "internship"
    assert classify_position("Graduate Traineeship") == "internship"
    assert classify_position("Tirocinio di ricerca in design") == "internship"
    assert classify_position("Praktikum im Forschungslabor") == "internship"


def test_german_student_helpers_are_assistantships():
    assert classify_position("stud. Hilfskraft (m/w/d)(5h/Woche)") == "assistantship"
    assert classify_position("Wissenschaftliche Hilfskraft gesucht") == "assistantship"


def test_plain_english_stage_does_not_look_like_an_internship():
    assert classify_position("Stage 2 selection results") == "other"


def test_italian_teaching_and_research_contracts_are_first_class_types():
    assert classify_position("Contratto di insegnamento in Fluidodinamica") == "faculty"
    assert classify_position("Incarico di ricerca in Fluidodinamica") == "research_staff"
    assert classify_position("Borsa di ricerca in design navale") == "research_fellowship"


def test_research_associates_and_conference_grants_are_first_class_types():
    assert classify_position("Research Associate in Fluid Dynamics") == "research_staff"
    assert classify_position("Ayudas para participación en congresos") == "research_fellowship"
    assert classify_position("Conference travel grants for doctoral researchers") == "research_fellowship"
    assert classify_position("Kingston University PhD studentships") == "research_fellowship"
    assert classify_position("ETH Career Seed Awards") == "research_fellowship"
    assert classify_position("Premios Madrid Accesible") == "research_fellowship"


def test_integrative_teaching_contracts_are_faculty_positions():
    assert (
        classify_position(
            "Avviso per 36 contratti integrativi di insegnamenti ufficiali"
        )
        == "faculty"
    )


def test_predoctoral_scholarships_remain_phd_positions():
    assert classify_position("Beca predoctoral en inteligencia artificial") == "phd"
