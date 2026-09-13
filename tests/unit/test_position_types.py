import pytest

from phd_searcher.position_types import classify_position


@pytest.mark.parametrize(
    "title",
    [
        "PhD fellowship in Biomolecular Native Mass Spectrometry",
        "Ph.D. scholarship in Interaction Design",
        "Fully funded doctoral studentship on human-AI interaction",
        "Predoctoral fellowship at a robotics laboratory",
    ],
)
def test_subject_qualified_doctoral_funding_offer_is_phd(title):
    assert classify_position(title) == "phd"


@pytest.mark.parametrize(
    "title",
    [
        "PhD scholarships",
        "Kingston University PhD studentships",
        "Conference travel grants for doctoral researchers",
        "How to apply for a PhD fellowship in biology",
        "PhD fellowship",
        "PhD fellowship for travel to a conference",
        "Scholarships for PhD candidates",
    ],
)
def test_doctoral_funding_directories_and_travel_are_not_promoted(title):
    assert classify_position(title) == "research_fellowship"


def test_doctoral_offer_rule_does_not_override_explicit_or_other_specific_roles():
    title = "PhD fellowship in biology"
    assert classify_position(title, explicit="research_fellowship") == "research_fellowship"
    assert classify_position("Postdoctoral fellowship in biology") == "postdoc"
    assert classify_position("Research Internship: PhD fellowship in biology") == "internship"
    assert classify_position("Funding opportunities", title) == "research_fellowship"


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
    assert classify_position("Avviso per 36 contratti integrativi di insegnamenti ufficiali") == "faculty"


def test_predoctoral_scholarships_remain_phd_positions():
    assert classify_position("Beca predoctoral en inteligencia artificial") == "phd"


@pytest.mark.parametrize("title", [
    "User Research Officer", "Bioinformatician", "Biological Curator - Chemical Biology Resources",
    "Plant Genomics and Variation Team Leader",
])
def test_scientific_roles_are_not_degrees_or_funding_mentions(title):
    assert classify_position(title, "You must have a PhD. We also host postdoctoral fellowships.") == "research_staff"


@pytest.mark.parametrize("title", [
    "Full Stack Developer", "Outreach and Engagement Officer", "Digital Transformation Specialist",
    "Senior Site Reliability Engineer", "Kitchen Team Leader",
])
def test_named_nonresearch_jobs_do_not_borrow_degree_or_colleague_roles(title):
    assert classify_position(title, "A PhD is required. You will work with doctoral students and postdoctoral fellows.") == "other"
