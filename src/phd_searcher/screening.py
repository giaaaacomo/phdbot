"""Conservative, reversible pre-screening of scraped position candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass

from phd_searcher.pipeline.review_context import opportunity_kind_evidence_supports

SCREENING_STATUSES = ("pending", "eligible", "review", "rejected", "quarantine")


@dataclass(frozen=True, slots=True)
class ScreeningDecision:
    status: str
    reason: str


_SPACE = re.compile(r"\s+")
_VACANCY_SIGNAL = re.compile(
    r"\b(?:"
    r"position|vacanc(?:y|ies)|opening|job|recruitment|fellow(?:ship)?|grants?|"
    r"scholarships?|studentship|assistantship|internships?|interns?|traineeships?|trainees?|"
    r"post[ -]?doc(?:toral)?|researcher|"
    r"research scientist|research engineer|professor|lecturer|doctoral candidate|"
    r"phd candidate|call for applications?|bando|concorso|assegn[oi] di ricerca|"
    r"stellenangebot|wissenschaftliche[rs]? mitarbeiter|doktorand|tutor"
    r")\b",
    re.I,
)
_VACANCY_URL = re.compile(
    r"/(?:jobs?|vacanc(?:y|ies)|careers?|open[-_]?positions?|opportunities|recruitment)(?:/|[-_.?]|$)",
    re.I,
)
_CLOSED_SIGNAL = re.compile(
    r"^(?:we\s+)?(?:currently\s+)?(?:have\s+)?no(?![.\s-]*\d)\s+"
    r"(?:current(?:ly)?\s+)?(?:open\s+)?"
    r"(?:(?:phd|doctoral|post[ -]?doctoral|research)\s+)?"
    r"(?:positions?|vacanc(?:y|ies)|openings?)\b|"
    r"^(?:we\s+are\s+)?(?:currently\s+)?not accepting\s+(?:applications?|candidates?)\b|"
    r"\b(?:positions?|vacanc(?:y|ies)|openings?)\b.{0,35}\b(?:are|is|now)\s+(?:closed|filled)\b",
    re.I,
)
_EXPLICIT_STATUS_CLOSED = re.compile(
    r"\b(?:status|estado)\s*:\s*(?:closed|expired|filled|cerrad[oa]s?)\b",
    re.I,
)
_NAVIGATION_TITLES = {
    "about",
    "about the school",
    "about us",
    "aau phd career hub",
    "accommodation",
    "academic vacancies",
    "advanced job search",
    "aktuelles",
    "aktuelle stellenangebote",
    "academics",
    "admissions",
    "alumni",
    "apply",
    "ausbildung",
    "bandi di gara e contratti",
    "baremo",
    "beratung",
    "bewerber*innenmanagement",
    "blog",
    "careers",
    "contacts",
    "contact",
    "contact us",
    "courses",
    "criteri di valutazione",
    "current opportunities",
    "current vacancies",
    "datenschutz im bewerbungsverfahren",
    "downloads",
    "education",
    "entry requirements",
    "events",
    "euraxess - european job portal",
    "faq",
    "faqs",
    "fellowships",
    "footer memberships",
    "home",
    "hochschulsport",
    "international",
    "internships",
    "job offers",
    "job opportunities",
    "job portal of tu darmstadt",
    "job vacancies",
    "jobs",
    "jobs by email",
    "jobs by rss",
    "kontakt",
    "learn more",
    "líneas y equipos de investigación",
    "memoria verificada",
    "modello di domanda",
    "nomina commissione",
    "open positions",
    "overview",
    "page not found",
    "partners",
    "postdocs",
    "professuren",
    "promotionsausschuss",
    "news",
    "people",
    "phd opportunities",
    "phd vacancies",
    "postdoctoral opportunities",
    "programmes",
    "programs",
    "read more",
    "recruitment",
    "research",
    "research opportunities",
    "services",
    "stellenangebote",
    "stipendien",
    "studierende",
    "study",
    "students",
    "the recruitment and selection lifecycle",
    "veranstaltungen",
    "vacancies",
    "vacancies and how to apply",
    "view all vacancies",
    "weiterbildung",
    "wissenschaftliches personal",
    "work with us",
    "wohnen",
}
_DEGREE_TITLE = re.compile(
    r"^(?:"
    r"duales?\s+studium\b|"
    r"(?:double|dual|doble)\s+(?:bachelor|degree|grado)\b|"
    r"(?:bachelor|bsc|undergraduate degree)\s+(?:in|of)\b|"
    r"(?:master(?:'s)? degree|msc)\s+(?:in|of)\b|"
    r"(?:grado|doble grado|licenciatura)\s+en\b|"
    r"(?:máster|master universitario|mestrado)\s+en\b|"
    r"(?:corso di\s+)?laurea\b|"
    r"laurea\s+(?:triennale|magistrale)\b|"
    r"master universitario\b|"
    r"(?:bachelor|master)studiengang\b|studiengang\b|"
    r"licence\s+(?:en|de)\b|"
    r"освітня\s+програма\b"
    r")",
    re.I,
)
_PROGRAMME_MARKETING = re.compile(
    r"^(?:course|degree|curriculum|tuition|fees?|fee status)\b|"
    r"^(?:study|academic)\s+programmes?\b|^(?:study|academic)\s+programs?\b|"
    r"^(?:double|dual)\s+degree\b|"
    r"\b(?:online|part[- ]time|full[- ]time)\s+(?:degree|course|programme?|program)\b",
    re.I,
)
_STRONG_RECRUITMENT_SIGNAL = re.compile(
    r"\b(?:"
    r"vacanc(?:y|ies)|job openings?|recruit(?:ment|ing)|hiring|"
    r"(?:ph\.?d|doctoral|postdoctoral|research|faculty) positions?|"
    r"offer of (?:a )?(?:pre[- ]?doctoral|ph\.?d|research) (?:place|position)|"
    r"call for applications?|employment contracts?|salary|stipend|"
    r"(?:send|submit) (?:a |your )?c\.?v"
    r")\b",
    re.I,
)
_ACTIONABLE_APPLICATION_WINDOW = re.compile(
    r"\b(?:"
    r"applications?\b[^.!?]{0,100}\b(?:are|is)\s+(?:now|currently)\s+open|"
    r"applications?\s+(?:are\s+)?accepted\b|"
    r"application\s+deadline\b|closing\s+date\b|"
    r"(?:apply|submit(?:\s+(?:an?|your))?\s+applications?)\s+"
    r"(?:now\s+)?(?:by|before|until|no later than)\b"
    r")",
    re.I,
)
_STUDENT_LIFECYCLE_TITLE = re.compile(
    r"^(?:"
    r"enrol?lment|"
    r"change of (?:the )?(?:director|supervisor) of (?:the )?thesis|"
    r"extension of (?:the )?thesis reading deadline|"
    r"interruption(?:/leave)? and readmission to doctoral studies|"
    r"application for admission to thesis defen[cs]e(?: and proposal of tribunal)?|"
    r"co[- ]authored thesis|other requests"
    r")$",
    re.I,
)
_STUDENT_LIFECYCLE_BODY = re.compile(
    r"\b(?:ph\.?d students?|doctoral students?|thesis|doctoral dissertation)\b",
    re.I,
)
_STUDENT_ADMIN_ACTION = re.compile(
    r"\b(?:form|register|enrol?lment|fees?|academic committee|tribunal|"
    r"defen[cs]e|readmission|supervision agreement|thesis director)\b",
    re.I,
)
_GOVERNANCE_TITLE = re.compile(
    r"^(?:(?:committee|board) of ethics(?: \([^)]*\))?|"
    r"(?:rules and (?:ministerial )?regulations|regulations of .+))$",
    re.I,
)
_GOVERNANCE_BODY = re.compile(
    r"\b(?:assessed and approved|ethical assessment|rules and regulations|"
    r"code of good practices|internal regulations|statute|protocol)\b",
    re.I,
)
_AWARD_TITLE = re.compile(r"^(?:conditions for granting|procedure)$", re.I)
_AWARD_BODY = re.compile(r"\bextraordinary (?:award|prize)s?\b", re.I)
_AWARD_CORROBORATION = re.compile(
    r"\b(?:tribunal|approved thesis|academic certificate)\b",
    re.I,
)
_THESIS_PRIZE_TITLE = re.compile(
    r"^(?=.*\b(?:premi?|prizes?|awards?)\b)"
    r"(?=.*\b(?:tesi|thes(?:is|es)|dissertations?)\b).+$",
    re.I,
)
_INTERNATIONAL_DOCTORATE_TITLE = re.compile(r"^international doctorate$", re.I)
_INTERNATIONAL_DOCTORATE_BODY = re.compile(
    r"\b(?:once the thesis has been defended|accredited specialization|"
    r"title of doctor will include)\b",
    re.I,
)
_DETAIL_CLOSED_SIGNAL = re.compile(
    r"\b(?:status|application status|call status|estado)\s*[:\-]\s*"
    r"(?:closed|expired|filled|cerrad[oa]s?)\b|"
    r"\b(?:applications?|the call)\s+(?:is|are|has been|have been)\s+"
    r"(?:now\s+)?(?:closed|expired|filled)\b|"
    r"\bdeadline\s+(?:has\s+)?(?:passed|expired)\b|"
    r"\bno longer accept(?:s|ing)(?:\s+applications?)?\b|"
    r"\b(?:ha\s+)?finalizado\s+el\s+plazo\s+(?:para|de)\s+"
    r"la\s+presentaci[oó]n\s+de\s+solicitudes\b",
    re.I,
)
_DETAIL_CLOSED_CONTRADICTION = re.compile(
    r"\b(?:not|isn't|is not|aren't|are not|never)\s+(?:currently\s+)?"
    r"(?:closed|expired|filled)\b|"
    r"\b(?:applications?|call|position)\s+(?:is|are|remain|remains)\s+"
    r"(?:still\s+)?open\b|"
    r"\bno\s+(?:ha\s+)?finalizado\s+el\s+plazo\b|"
    r"\bel\s+plazo\b.{0,35}\bno\s+(?:ha\s+)?finalizado\b",
    re.I,
)
_SYSTEM_OR_ACCESS_TITLE = re.compile(
    r"^(?:making sure you(?:'|\u2019)re not a bot!?|"
    r"die webseite ist aktuell nicht erreichbar\..*|"
    r"the website is currently not available\..*)$",
    re.I,
)
_SELECTION_RESULT_TITLE = re.compile(
    r"^(?:graduator(?:ia|ie)\s+(?:finale|definitiv[ae]).*|"
    r"resoluci[oó]n\s+(?:definitiva|provisional)\s+de\s+.+)$",
    re.I,
)
_NON_OPPORTUNITY_PAGE_TITLE = re.compile(
    r"^(?:online form \(enrolment\b.*|payment by transfer|webinar recording|"
    r"ph\.?d brochure|.*programme outline.*|how does it work\??|"
    r"research support(?: \(rs\))?|relations internationales|"
    r".*\binfo and networking event\b.*|"
    r"how to apply|information for|scholarships?|sport scholarships?|"
    r"(?:tuition\s+)?fees?(?:\s*,\s*|\s+(?:and|&)\s+)funding"
    r"(?:\s+(?:and|&)\s+scholarships?)?|"
    r"fees?\s+(?:and|&)\s+(?:scholarships?|grants?)|"
    r".*\bcall for papers?\b.*|"
    r"the semesters|administrative vacancies|operational vacancies|benefits|"
    r"unlocking edinburgh|roles|ansprechperson|discounts|teaching|"
    r"stays\s*/\s*mobility|legislation and rules and regulations|"
    r"foreign staff with pre[- ]doctoral contracts|procedure personale docente|"
    r"funding for prospective postgraduate researchers|"
    r"modulistica per il riconoscimento dei titoli all'estero.*|"
    r"what will you study on .*|allgemeine informationen|"
    r"habilitationsverfahren\s*-\s*ablauf|publikationen|"
    r"tagungen, workshops und summer schools|verbundforschung|"
    r"universitätsbund tübingen e\.v\.|estudiar en la us|eipd|"
    r"ph\.?d guide.*|guide:\s*(?:the\s+)?ph\.?d journey.*|ph\.?d guidelines|"
    r"internal regulations|programme details|research priority areas|"
    r"iris ph\.?d thesis archive|interne tools|für studierende|"
    r"staff profile|need more information\??|frequently[- ]asked questions)$",
    re.I,
)
_CATEGORY_FRAGMENT_TITLE = re.compile(
    r"^(?:chiedi informazioni|bandi|bandi (?:docenza|contratti|premi|borse|scuola).*|"
    r"borse di ricerca|incarichi di ricerca|concorsi a posti di .*|"
    r"corsi di .*|master universitari|scuole di specializzazione|"
    r"summer e winter school|formazione insegnanti|percorsi minor|"
    r"procedure per la chiamata professori di i e ii fascia|"
    r"concorsi a professori straordinari|"
    r"procedure di mobilità per la chiamata di professori di i e ii fascia|"
    r"trasferimento per ricercatori .*|"
    r"abilitazione scientifica nazionale .*|ricercatori a tempo determinato|"
    r"mobilità all'estero .*|visiting professors, researchers, scholars and fellows .*|"
    r"premi di ricerca|incarichi di (?:insegnamento|tutorato|formazione linguistica|collaborazione)|"
    r"contratti di ricerca|"
    r"borse di studio per il proseguimento della formazione di giovani laureati|"
    r"bandi di concorso .*|"
    r"gare di (?:appalto|vendita)|avvisi di (?:selezione|sponsorizzazione).*)$",
    re.I,
)


def _clean(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


def detail_rejection_evidence(description: str) -> str | None:
    """Return a short explicit closure quote, never an inferred status."""
    text = _clean(description)
    for match in _DETAIL_CLOSED_SIGNAL.finditer(text):
        window = text[max(0, match.start() - 80) : min(len(text), match.end() + 80)]
        if not _DETAIL_CLOSED_CONTRADICTION.search(window):
            return window[:500]
    return None


def screen_enriched_position(
    title: str,
    url: str,
    description: str,
    position_type: str = "other",
) -> ScreeningDecision:
    """Guard an enriched eligible record against explicit contradictions."""
    decision = screen_position(title, url, description, position_type)
    if decision.status == "rejected" and decision.reason in {
        "explicitly_closed_or_unavailable",
        "selection_result_page",
        "non_opportunity_page",
        "navigation_link",
        "administrative_non_vacancy_page",
    }:
        return decision
    if detail_rejection_evidence(description):
        return ScreeningDecision("rejected", "detail_explicitly_closed")
    if decision.status == "rejected":
        return ScreeningDecision("eligible", "detail_no_explicit_contradiction")
    return decision


def _is_corroborated_administrative_page(title: str, description: str) -> bool:
    """Identify non-vacancy administration only when title and body agree.

    Generic words such as ``procedure`` or ``enrollment`` are unsafe by
    themselves: elsewhere in the catalogue they also occur in real job titles.
    Full-text corroboration keeps this rule deterministic without weakening the
    evidence validator used by the LLM stages.
    """
    body = _clean(description)
    if not body or _STRONG_RECRUITMENT_SIGNAL.search(f"{title}\n{body}"):
        return False
    if _STUDENT_LIFECYCLE_TITLE.fullmatch(title):
        return bool(
            _STUDENT_LIFECYCLE_BODY.search(body)
            and _STUDENT_ADMIN_ACTION.search(body)
        )
    if _GOVERNANCE_TITLE.fullmatch(title):
        return bool(_GOVERNANCE_BODY.search(body))
    if _AWARD_TITLE.fullmatch(title):
        return bool(
            _AWARD_BODY.search(body)
            and re.search(r"\bthes(?:is|es)\b", body, re.I)
            and _AWARD_CORROBORATION.search(body)
        )
    if _INTERNATIONAL_DOCTORATE_TITLE.fullmatch(title):
        return bool(_INTERNATIONAL_DOCTORATE_BODY.search(body))
    return False


def _is_high_confidence_non_opportunity(title: str, url: str, description: str) -> bool:
    """Recognize pages and category headings that cannot be a concrete vacancy."""
    body = _clean(description)
    if _SYSTEM_OR_ACCESS_TITLE.fullmatch(title):
        return True
    if _THESIS_PRIZE_TITLE.fullmatch(title):
        # A prize for an already completed thesis can be academically useful,
        # but it is neither a study/research position nor a programme intake.
        return True
    if _NON_OPPORTUNITY_PAGE_TITLE.fullmatch(title):
        # This is a closed, audited list of exact page/category titles. Their
        # fetched HTML often contains unrelated vacancy words in navigation or
        # neighbouring cards.  Preserve the exceptional case where the record
        # itself carries both a concrete opportunity and an actionable/current
        # application window; a generic label must not erase that evidence.
        return not _has_concrete_opportunity_details(body)
    if "#" in url and _CATEGORY_FRAGMENT_TITLE.fullmatch(title):
        # Listing/category accordions have repeatedly been emitted as records.
        # Restrict this to title-only fragments so an actual detailed call with
        # a similar heading is never discarded.
        return not body or body.casefold() == title.casefold()
    return False


def _has_concrete_opportunity_details(description: str) -> bool:
    """Require independent vacancy and application-window evidence from the body.

    A lone ``scholarship`` or ``apply now`` in a menu is deliberately
    insufficient.  This narrow conjunction protects real calls embedded below
    generic headings (for example ``Open positions``) while retaining the
    high-precision rejection of navigation/category pages.
    """
    body = _clean(description)
    return bool(
        body
        and _VACANCY_SIGNAL.search(body)
        and _ACTIONABLE_APPLICATION_WINDOW.search(body)
    )


def screen_position(
    title: str,
    url: str,
    description: str = "",
    position_type: str = "other",
) -> ScreeningDecision:
    """Classify without fetching the detail page.

    Only high-confidence non-opportunities are rejected. Ambiguous candidates
    remain visible in the manual review queue and can be approved at any time.
    """
    clean_title = _clean(title)
    folded = clean_title.casefold().strip(" .:;-|")
    has_vacancy_signal = bool(
        _VACANCY_SIGNAL.search(clean_title) or _VACANCY_URL.search(url)
    )

    if not folded:
        return ScreeningDecision("review", "empty_title")
    if _EXPLICIT_STATUS_CLOSED.search(clean_title) or _CLOSED_SIGNAL.search(clean_title):
        return ScreeningDecision("rejected", "explicitly_closed_or_unavailable")
    if _SELECTION_RESULT_TITLE.fullmatch(clean_title):
        # An outcome document remains non-searchable even when it embeds the
        # original call and deadline; unlike a generic heading, it cannot be
        # rescued by those historical application details.
        return ScreeningDecision("rejected", "selection_result_page")
    if _is_high_confidence_non_opportunity(clean_title, url, description):
        return ScreeningDecision("rejected", "non_opportunity_page")
    if folded in _NAVIGATION_TITLES and not _has_concrete_opportunity_details(
        description
    ):
        return ScreeningDecision("rejected", "navigation_link")
    if (
        not has_vacancy_signal
        and _is_corroborated_administrative_page(clean_title, description)
    ):
        return ScreeningDecision("rejected", "administrative_non_vacancy_page")
    if (
        _DEGREE_TITLE.search(clean_title) or _PROGRAMME_MARKETING.search(clean_title)
    ) and not has_vacancy_signal:
        if opportunity_kind_evidence_supports(
            [clean_title, description],
            "programme",
        ):
            return ScreeningDecision("review", "current_or_future_programme_intake")
        return ScreeningDecision("rejected", "degree_or_course_page")
    if position_type != "other":
        if has_vacancy_signal:
            return ScreeningDecision("eligible", f"recognized_type:{position_type}")
        return ScreeningDecision(
            "review", f"recognized_type_without_vacancy_signal:{position_type}"
        )
    if has_vacancy_signal:
        return ScreeningDecision("eligible", "vacancy_signal")
    return ScreeningDecision("review", "ambiguous_candidate")
