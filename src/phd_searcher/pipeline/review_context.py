"""Costruzione di un contesto breve ma informativo per la review profonda."""

from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urlsplit

from phd_searcher.clock import local_today
from phd_searcher.opportunity_kinds import (
    INFORMATION,
    PROGRAMME,
    SPONTANEOUS,
    UNKNOWN,
    VACANCY,
    OpportunityKind,
)
from phd_searcher.pipeline.normalize import extract_deadline, parse_dates, parse_deadline

_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"\w+", re.UNICODE)
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]+)\]\([^)]*\)")
_MARKDOWN_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_MIN_QUOTE_CHARS = 10
_MIN_QUOTE_WORDS = 2
_MAX_QUOTE_CHARS = 500
_UNUSABLE_DOCUMENT = re.compile(
    r"^\s*(?:```(?:json)?\s*)?\{\s*[\"']?error_type[\"']?\s*:|"
    r"\b(?:languagefolder|captcha|access denied|page not found)\b",
    re.I,
)
_EURAXESS_JOB_PATH = re.compile(r"^/jobs/\d+/?$")
_EURAXESS_JOB_INFORMATION = re.compile(r"\s+##\s+Job Information\b", re.I)
_EURAXESS_SHARE_FOOTER = re.compile(
    r"\s+#{4,6}\s+Share this page\b",
    re.I,
)
_EVIDENCE_SIGNAL = re.compile(
    r"\b(?:"
    r"apply|application|applications|submi(?:t|ts|tted|tting)|deadlines?|closing dates?|open until|start date|"
    r"vacanc(?:y|ies)|positions?|opening|doctoral|doctorate|ph\.?d|postdoc|"
    r"assistantships?|internships?|traineeships?|fellowships?|scholarships?|grants?|awards?|"
    r"research associate|contract|salary|stipend|duration|"
    r"closed|filled|expired|not accepting|no vacancies|"
    r"candidatur[ae]|scadenza|bando|posizione|contratto|compenso|durata|chius[oa]|"
    r"bewerbung(?:en)?|bewerbungsfrist|stelle|vertrag|vergütung|geschlossen|"
    r"candidature|date limite|poste|contrat|rémunération|clôtur|"
    r"solicitudes?|convocatoria|ayudas?|becas?|premios?|inscripci[oó]n|plazo de presentación|"
    r"fecha límite|puesto|contrato|remuneración|cerrad[oa]"
    r")\b",
    re.I,
)
_APPLICATION_ACTION_SIGNAL = re.compile(
    r"\b(?:how to apply|apply now|apply using|start (?:an?|your) application|"
    r"application portal|submit (?:an?|your|the) application)\b",
    re.I,
)
_DECISIVE_STATUS_SIGNAL = re.compile(
    r"\b(?:application deadline|closing date|application status|call status|"
    r"applications? (?:are|is|have been|has been) (?:now )?(?:open|closed)|"
    r"no longer accept(?:s|ing)|deadline (?:has )?(?:passed|expired))\b",
    re.I,
)
_VACANCY_SIGNAL = re.compile(
    r"\b(?:vacanc(?:y|ies)|opening|job|role|positions?|opportunit(?:y|ies)|"
    r"doctoral|doctorate|ph\.?d|postdoc|internships?|interns?|traineeships?|trainees?|assistantship|"
    r"(?:studentische|wissenschaftliche|stud\.?)\s+hilfskr(?:aft|äfte)|"
    r"studentships?|fellowships?|scholarships?|grants?|awards?|prizes?|premios?|funding calls?|"
    r"research(?:er| staff| scientist| engineer| associate)|faculty|lecturer|professor|"
    r"bando|avviso (?:pubblico )?(?:di selezione|di interesse)|"
    r"selezion[ei] pubblic(?:a|o|he|i)|incaric(?:o|hi)|carichi? di didattica|"
    r"contratt[oi] (?:di ricerca|di insegnamento)|posizione|posto|"
    r"convocatoria|becas?|ayudas?|"
    r"stelle|poste|puesto|vacante|offre|emploi)\b",
    re.I,
)
_APPLICATION_SIGNAL = re.compile(
    r"\b(?:apply|application|applications|applicant|candidate|submi(?:t|ts|tted|tting)|deadlines?|"
    r"closing dates?|open until|open from|invited|candidatur[ae]|scadenza|domanda|"
    r"bewerbung(?:en)?|bewerbungsfrist|candidature|date limite|postuler|solicitudes?|"
    r"convocatoria|inscripci[oó]n(?:es)?|plazo de presentación|fecha límite|presentar|recruiting now|"
    r"will open|opens? in|forthcoming|upcoming)\b",
    re.I,
)
_RELATIVE_PUBLICATION_WINDOW_SIGNAL = re.compile(
    r"(?:\b(?:within|no later than)\b.{0,140}\b(?:publication|posting|announcement|notice)\b|"
    r"\bentro\b.{0,140}\bpubblicazion\w*\b|"
    r"\binnerhalb\b.{0,140}\bver[oö]ffentlich\w*\b|"
    r"\bdans\s+un\s+d[eé]lai\b.{0,140}\bpublication\b|"
    r"\b(?:dentro\s+de|en\s+el\s+plazo\s+de)\b.{0,140}"
    r"\b(?:publicaci[oó]n|anuncio|convocatoria)\b)",
    re.I,
)
_CURRENT_APPLICATION_SIGNAL = re.compile(
    r"\b(?:apply\s+now|open\s+for\s+applications?|currently\s+accepting\s+applications?|"
    r"applications?\s+(?:for\b.{0,80}\s+)?(?:are|remain)\s+"
    r"(?:currently\s+|now\s+|still\s+)?open|"
    r"applications?\s+(?:are|is)\s+(?:currently\s+|now\s+)?invited|"
    r"candidates?\s+(?:are|is)\s+(?:currently\s+|now\s+)?invited\s+to\s+apply|"
    r"(?:requests?|applications?)\s+(?:can|may)\s+be\s+submitted\s+(?:at\s+any\s+time|anytime)|"
    r"(?:send|submit)\s+(?:an?\s+|your\s+)?application\s+directly\s+to\s+"
    r"(?:the\s+)?(?:head|supervisor|principal investigator|research group|department)|"
    r"candidatur[ae]\s+(?:sono\s+)?apert[ae]|candidatures?\s+(?:sont\s+)?ouvertes?|"
    r"(?:solicitudes?|candidaturas?|inscripciones?)\s+(?:est[aá]n\s+)?abiert[ao]s?|"
    r"bewerbung(?:en)?\s+(?:sind\s+)?(?:offen|ge[oö]ffnet))\b",
    re.I,
)
_ABSOLUTE_PUBLICATION_ANCHOR_SIGNAL = re.compile(
    r"\b(?:publication(?:\s+date)?|published(?:\s+on)?|posting\s+date|posted(?:\s+on)?|"
    r"data\s+di\s+pubblicazione|pubblicat[oa]\s+il|"
    r"ver[oö]ffentlichungsdatum|ver[oö]ffentlicht\s+am|"
    r"date\s+de\s+publication|publi[eé]\s+le|"
    r"fecha\s+de\s+publicaci[oó]n|publicad[oa]\s+el)\b",
    re.I,
)
_ABSOLUTE_DEADLINE_ANCHOR_SIGNAL = re.compile(
    r"\b(?:application\s+deadline|submission\s+deadline|registration\s+deadline|"
    r"closing\s+date|apply\s+by|application\s+portal.{0,80}\bcloses?|scadenza|"
    r"bewerbungsfrist|date\s+limite|fecha\s+l[ií]mite|plazo\s+de\s+presentaci[oó]n)\b",
    re.I,
)
_RELATIVE_WINDOW_DAYS_SIGNAL = re.compile(
    r"(?:\b(?:within|no\s+later\s+than)\b|\bentro\b|\binnerhalb\b|"
    r"\bdans\s+un\s+d[eé]lai\b|\b(?:dentro\s+de|en\s+el\s+plazo\s+de)\b)"
    r"(?P<window>.{0,80}?)\b(?:days?|giorni|tage[n]?|jours?|d[ií]as)\b"
    r".{0,100}?\b(?:publication|posting|announcement|notice|pubblicazion\w*|"
    r"ver[oö]ffentlich\w*|publicaci[oó]n|anuncio|convocatoria)\b",
    re.I,
)
_PAST_APPLICATION_INVITATION_SIGNAL = re.compile(
    r"\bapplications?\s+(?:were|was|had\s+been)\s+invited\b",
    re.I,
)
_EXPLICIT_FUTURE_APPLICATION_SIGNAL = re.compile(
    r"\b(?:applications?.{0,80}\b(?:will\s+(?:re)?open|opens?\s+in)|"
    r"application\s+window.{0,80}\bwill\s+(?:re)?open|"
    r"forthcoming\s+(?:application\s+)?(?:window|intake)|next\s+intake|"
    r"rolling\s+admissions?|year[- ]round\s+applications?|"
    r"applications?.{0,80}\bthroughout\s+(?:the\s+)?(?:whole\s+)?year\b)\b",
    re.I,
)
_NULL_DEADLINE_SIGNAL = re.compile(
    r"\b(?:application\s+deadline|closing\s+date|deadline)\b"
    r"(?:\s|[*:\u2013\u2014_-]){0,16}"
    r"(?:none(?:\s+specified)?|not\s+specified|n\s*/?\s*a|no\s+deadline)\b",
    re.I,
)
_DEADLINE_SIGNAL = re.compile(
    r"\b(?:application deadline|submission deadline|registration deadline|closing date|"
    r"application portal.{0,160}\bcloses?|apply(?:ing)? (?:no later than|by)|"
    r"applications? (?:close|until|between)|open (?:until|till)|scadenza|"
    r"bewerbungsfrist|bewerbung(?:en)?.{0,160}\bbis(?:\s+zum)?|date limite|"
    r"fecha límite|plazo de presentación|formalización de solicitudes|"
    r"(?:apply|applications?|solicitar|solicitudes?|candidatur[ae]|bewerbung)"
    r".{0,160}(?:by|until|between|entre|desde|del|hasta|avant|bis))\b",
    re.I,
)
_NON_VACANCY_SIGNAL = re.compile(
    r"\b(?:no vacancies|not a vacancy|no (?:open )?positions|"
    r"degree information|archive|generic information|guide(?:lines)?|"
    r"internal regulations|rules and regulations|thesis archive|staff profile|"
    r"maternity leave|admission to thesis defen[cs]e|research priority areas|"
    r"nessun[ao] (?:posizione|posto)|"
    r"keine (?:stelle|stellen)|aucun poste|sin vacantes)\b",
    re.I,
)
_CLOSED_SIGNAL = re.compile(
    r"\b(?:closed|expired|filled|not accepting|no longer accept(?:s|ing)|deadline (?:has )?passed|"
    r"applications?\s+(?:are|is)\s+not\s+(?:currently\s+)?open|"
    r"not\s+(?:currently\s+)?open\s+for\s+applications?|"
    r"chius[oa]|scadut[oa]|conclus[oa]|geschlossen|abgelaufen|clôtur|expir|"
    r"cerrad[oa]|vencid[oa])\b",
    re.I,
)
_CLOSED_CONTRADICTION = re.compile(
    r"\b(?:not|isn't|is not|aren't|are not|never)\s+(?:currently\s+)?(?:closed|expired|filled)\b|"
    r"\b(?:applications?|call|position)\s+(?:is|are|remain|remains)\s+(?:still\s+)?open\b|"
    r"\b(?:remains?|still|currently)\s+open\b",
    re.I,
)
_OPEN_NEGATION_SIGNAL = re.compile(
    r"\bapplications?\s+(?:are|is)\s+not\s+(?:currently\s+)?open\b|"
    r"\bnot\s+(?:currently\s+)?open\s+for\s+applications?\b",
    re.I,
)
_EXPLICIT_CLOSED_SIGNAL = re.compile(
    r"\b(?:status|application status|call status|stato|estado)\s*[:\-]\s*"
    r"(?:closed|expired|filled|scadut[oa]|chius[oa]|cerrad[oa])\b|"
    r"^\s*(?:closed|expired|filled|scadut[oa]|chius[oa]|cerrad[oa])\s*$|"
    r"\b(?:applications?|call)\s+(?:is|are|has been|have been)\s+(?:now\s+)?"
    r"(?:closed|expired|filled)\b|"
    r"\bdeadline\s+(?:has\s+)?(?:passed|expired)\b|"
    r"\bno longer accept(?:s|ing)(?:\s+applications?)?\b",
    re.I,
)
_EXPLICIT_NON_VACANCY_SIGNAL = re.compile(
    r"\bno (?:open )?(?:vacanc(?:y|ies)|positions?|openings?)\b|"
    r"\bnot (?:an?\s+)?(?:vacancy|position|opening)\b|"
    r"\b(?:vacanc(?:y|ies)|positions?|openings?)\s+(?:is|are)\s+not\s+available\b|"
    r"\bnessun[ao] (?:posizione|posto)\b|\bkeine (?:stelle|stellen)\b|"
    r"\baucun poste\b|\bsin vacantes\b",
    re.I,
)
_TYPE_SIGNALS: dict[str, re.Pattern[str]] = {
    "phd": re.compile(r"\b(?:ph\.?d|doctoral|doctorate|dottorat[oa]?|doktorat|doctorant)\b", re.I),
    "masters_mph": re.compile(r"\b(?:master|mph|mphil|master'?s thesis|tesi di laurea)\b", re.I),
    "medical_doctorate": re.compile(r"\b(?:md.?phd|medical doctorate|doctor of medicine)\b", re.I),
    "internship": re.compile(r"\b(?:internship|traineeship|intern|tirocin|praktikum|stage)\b", re.I),
    "assistantship": re.compile(
        r"\b(?:assistantship|research assistant|teaching assistant|assistente|"
        r"(?:studentische|wissenschaftliche|stud\.?)\s+hilfskr(?:aft|äfte))\b",
        re.I,
    ),
    "research_fellowship": re.compile(
        r"\b(?:fellowships?|research fellows?|studentships?|assegno di ricerca|borsa di ricerca|"
        r"scholarships?|awards?|prizes?|premios?|becas?|ayudas?|predoctoral grants?|grants?)\b",
        re.I,
    ),
    "postdoc": re.compile(r"\b(?:post.?doc|postdoctoral)\b", re.I),
    "research_staff": re.compile(
        r"\b(?:research staff|researcher|research scientist|research engineer|research associate|ricercatore|"
        r"incaric(?:o|hi) di ricerca|incaric(?:o|hi) di lavoro autonomo)\b",
        re.I,
    ),
    "faculty": re.compile(
        r"\b(?:faculty|lecturer|professor|professore|docent|contratt[oi] di insegnamento|"
        r"contratt[oi](?: integrativ[oi])? di insegnament[oi]|"
        r"incaric(?:o|hi) di insegnamento|carichi? di didattica)\b",
        re.I,
    ),
}
_CONCRETE_VACANCY_KIND_SIGNAL = re.compile(
    r"\b(?:vacanc(?:y|ies)|opening|job|role|positions?|selection process|public selection|"
    r"call for applications?|fixed[- ]term contract|tenure track|funded project|research project|"
    r"internships?|traineeships?|assistantships?|"
    r"interns?|trainees?|fellowships?|scholarships?|studentships?|grants?|awards?|prizes?|"
    r"(?:studentische|wissenschaftliche|stud\.?)\s+hilfskr(?:aft|äfte)|"
    r"research associate|postdoc(?:toral)?|we are seeking)\b|"
    r"\b(?:bando|concorso|avviso pubblico di selezione|selezion[ei] pubblic(?:a|o|he|i)|"
    r"contratt[oi] di ricerca|"
    r"contratt[oi] di insegnamento|incaric(?:o|hi) di (?:ricerca|insegnamento)|"
    r"assegn[oi] di ricerca|bors[ae] di ricerca|convocatoria|premios?|becas?|ayudas?|"
    r"puestos?|vacantes?)\b",
    re.I,
)
_SPECIFIC_PROJECT_VACANCY_SIGNAL = re.compile(
    r"\b(?:reference\s+number|pgr-[a-z]?[- ]?\d+|type\s+of\s+research\s+degree|"
    r"funding\s+(?:funded|unfunded))\b",
    re.I,
)
_PROGRAMME_KIND_SIGNAL = re.compile(
    r"\b(?:(?:ph\.?d|doctoral|doctorate|graduate|master'?s?|mph).{0,80}"
    r"(?:programmes?|programs?|school|admissions?|placements?)|"
    r"(?:programmes?|programs?|school).{0,80}"
    r"(?:ph\.?d|doctoral|doctorate|graduate|master'?s?|mph)|"
    r"(?:international\s+)?(?:graduate|research)\s+school|"
    r"integrated\s+research\s+training\s+group|cluster\s+of\s+excellence|"
    r"innovative\s+training\s+network)\b",
    re.I,
)
_PROGRAMME_ACTIONABLE_INTAKE_SIGNAL = re.compile(
    r"\b(?:"
    r"(?:applications?|admissions?)\s+(?:are\s+)?(?:now\s+|currently\s+|still\s+)?open\b|"
    r"apply\s+now\b|"
    r"next\s+intake\b|rolling\s+admissions?\b|year[- ]round\s+applications?\b|"
    r"applications?.{0,80}\bthroughout\s+(?:the\s+)?(?:whole\s+)?year\b|"
    r"applications?.{0,80}\bwill\s+open\b|applications?.{0,80}\bopens?\s+in\b"
    r")",
    re.I,
)
_PROGRAMME_DATED_INTAKE_SIGNAL = re.compile(
    r"\bapplications?\s+(?:for\s+the\s+)?"
    r"(?P<start_year>20\d{2})"
    r"(?:\s*[/\-\u2013\u2014]\s*(?P<end_year>\d{2}|20\d{2}))?"
    r"\s+intake\b",
    re.I,
)
_PROGRAMME_RECURRING_DEADLINE_SIGNAL = re.compile(
    r"\b(?:(?:application|admission)s?|submission)\s+deadlines?\b.{0,100}(?:"
    r"any\s*time|throughout\s+(?:the\s+)?year|year[- ]round|"
    r"(?:once|twice|each|every)\s+(?:a\s+)?year|"
    r"(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?"
    r".{0,50}\b(?:and|or)\b.{0,50}"
    r"(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?"
    r")",
    re.I,
)
_SPONTANEOUS_KIND_SIGNAL = re.compile(
    r"\b(?:unsolicited|speculative|spontaneous)\s+(?:applications?|candidatures?)\b|"
    r"\bexpressions?\s+of\s+interest\b|"
    r"\b(?:send|submit)\s+(?:an?|your)\s+application\s+directly\s+to\s+"
    r"(?:the\s+)?(?:head|supervisor|principal investigator|research group|department)\b|"
    r"\bcontact\s+(?:a|the|your)\s+(?:prospective\s+)?(?:supervisor|research group)\b.{0,100}"
    r"\b(?:apply|application)\b",
    re.I,
)
_INFORMATION_KIND_SIGNAL = re.compile(
    r"\b(?:general information|information for|information page|frequently asked questions|faq|"
    r"how to apply|application process|programme information|program information|course information|"
    r"guidance|guidelines|news|events?|archive)\b",
    re.I,
)
_OPPORTUNITY_KIND_EVIDENCE_ORDER: tuple[OpportunityKind, ...] = (
    SPONTANEOUS,
    PROGRAMME,
    VACANCY,
    INFORMATION,
)

_TYPOGRAPHIC_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
    }
)


def compact_text(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


def _grounding_text(value: str) -> str:
    """Normalize layout-only extraction artifacts without changing words.

    PDF extractors commonly emit ``( email@example.org)`` while the model
    copies the visually identical ``(email@example.org)``.  Only whitespace
    immediately inside brackets and invisible formatting characters are
    ignored; visible word order and semantic punctuation remain mandatory.
    """
    text = _MARKDOWN_LINK.sub(r"\1", value)
    # PDF/HTML extractors can inject invisible layout characters inside a
    # visually continuous word (for example ``non-\u200bpermanent``). They carry
    # no semantic content and are unsafe to require in a verbatim citation.
    text = text.translate({ord(char): None for char in "\u00ad\u200b\u200c\u200d\ufeff"})
    text = _MARKDOWN_HEADING.sub("", text)
    text = text.replace("`", "")
    # Ignore Markdown emphasis markers at token boundaries, including the
    # single ``_word_`` form emitted by some local-model citations. Preserve
    # underscores inside identifiers so normalization cannot change words.
    text = re.sub(r"(?<!\w)[*_]+|[*_]+(?!\w)", "", text)
    text = compact_text(text.translate(_TYPOGRAPHIC_TRANSLATION))
    quote_pairs = (("\"", "\""), ("'", "'"), ("«", "»"))
    if any(text.startswith(left) and text.endswith(right) for left, right in quote_pairs):
        text = text[1:-1].strip()
    text = re.sub(r"([([{])\s+", r"\1", text)
    text = re.sub(r"\s+([])}])", r"\1", text)
    return text.casefold()


def _euraxess_job_page(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.hostname == "euraxess.ec.europa.eu"
        and _EURAXESS_JOB_PATH.fullmatch(parsed.path) is not None
    )


def _euraxess_candidate_document(
    fetched: str,
    *,
    title: str,
    url: str,
) -> str | None:
    """Return one attributable EURAXESS job block without hiding its status.

    The candidate ends immediately before the social/share and site footer.
    ``STATUS: CLOSED/EXPIRED`` and the Apply link occur before that boundary on
    real pages, so they deliberately remain available to screening and review.
    """
    if not title.strip() or not _euraxess_job_page(url):
        return None

    folded = fetched.casefold()
    title_start = folded.find(compact_text(title).casefold())
    if title_start < 0:
        return None
    information = _EURAXESS_JOB_INFORMATION.search(fetched, title_start)
    if information is None:
        return None
    end = _EURAXESS_SHARE_FOOTER.search(fetched, information.end())
    if end is None:
        return None
    return fetched[title_start : end.start()].strip()


def has_future_deadline_status_conflict(
    description: str,
    full_description: str | None,
    *,
    title: str,
    url: str,
    deadline: date | None,
    deadline_raw: str | None = None,
    today: date | None = None,
) -> bool:
    """Detect a contradictory direct job page without deciding which side wins.

    This is intentionally an uncertainty signal, never proof that the call is
    open.  It requires a current/future candidate deadline, the candidate's
    own EURAXESS job block, a trailing explicit status, and a rendered Apply
    link.  Genuine body-level withdrawals therefore remain hard rejections.
    """
    current_day = today or local_today()
    if deadline is None or deadline < current_day:
        return False
    fetched = compact_text(full_description or "")
    candidate = _euraxess_candidate_document(
        fetched,
        title=title,
        url=url,
    )
    if candidate is None:
        return False
    contact = re.search(r"\s+##\s+Contact\b", candidate, re.I)
    if contact is None:
        return False
    trailing = candidate[contact.end() :]
    closed = _EXPLICIT_CLOSED_SIGNAL.search(trailing)
    if closed is None or not re.search(r"\[Apply now\]\(", trailing[closed.end() :], re.I):
        return False
    raw_deadline = compact_text(deadline_raw or "")
    if raw_deadline:
        if raw_deadline.casefold() not in candidate.casefold():
            return False
        candidate_deadline = parse_deadline(raw_deadline)
    else:
        _deadline_quote, candidate_deadline = extract_deadline(candidate)
    return candidate_deadline == deadline


def select_evidence_document(
    description: str,
    full_description: str | None,
    *,
    title: str | None = None,
    url: str | None = None,
    deadline: date | None = None,
    deadline_raw: str | None = None,
    today: date | None = None,
) -> str:
    """Prefer fetched detail text, but never hide usable inline evidence behind an error page.

    Some crawlers return a syntactically non-empty error payload (for example
    ``LanguageFolder``).  Treating that payload as the dossier discarded the
    candidate-specific description that was already stored by the listing
    scraper.  This fallback is deliberately narrow: ordinary short or
    ambiguous pages are not promoted into evidence.  When candidate identity
    is supplied, a direct EURAXESS page is reduced to its structurally
    attributable job block. Explicit CLOSED/EXPIRED markers remain inside that
    block; a separate uncertainty signal handles deadline/status conflicts.
    """
    inline = compact_text(description)
    fetched = compact_text(full_description or "")
    if not fetched or _UNUSABLE_DOCUMENT.search(fetched):
        return inline
    if title and url:
        candidate = _euraxess_candidate_document(
            fetched,
            title=title,
            url=url,
        )
        if candidate is not None:
            return candidate
    return fetched


def build_evidence_context(value: str, *, max_chars: int = 12_000) -> str:
    """Mantiene apertura, coda e finestre attorno ai segnali decisivi.

    Tagliare soltanto l'inizio nasconde spesso deadline e stato della call. Le
    finestre sono ordinate come nel documento e deduplicate prima del limite.
    """
    text = compact_text(value)
    if len(text) <= max_chars:
        return text

    head_size = min(1_600, max_chars // 4)
    tail_size = min(1_200, max_chars // 5)
    signal_budget = max(max_chars - head_size - tail_size - 10, 0)
    def spans_for(pattern: re.Pattern[str]) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for match in pattern.finditer(text):
            start = max(match.start() - 500, head_size)
            end = min(match.end() + 700, len(text) - tail_size)
            if start >= end:
                continue
            if spans and start <= spans[-1][1] + 80:
                spans[-1] = (spans[-1][0], max(spans[-1][1], end))
            else:
                spans.append((start, end))
        return spans

    # A long institutional page may mention "application" dozens of times in
    # menus before its project-specific Apply button near the end. Reserve the
    # middle budget for actionable instructions first, then status/deadline,
    # and only then generic evidence signals. Output is sorted back into source
    # order so the model still receives a coherent document.
    ranked_spans = (
        spans_for(_APPLICATION_ACTION_SIGNAL),
        spans_for(_DECISIVE_STATUS_SIGNAL),
        spans_for(_EVIDENCE_SIGNAL),
    )
    middle_parts: list[tuple[int, str]] = []
    consumed = 0
    selected_spans: list[tuple[int, int]] = []
    for spans in ranked_spans:
        for start, end in spans:
            if any(start <= selected_end + 80 and end >= selected_start - 80 for selected_start, selected_end in selected_spans):
                continue
            part = text[start:end].strip()
            separator = 5 if middle_parts else 0
            room = signal_budget - consumed - separator
            if room <= 0:
                break
            middle_parts.append((start, part[:room]))
            selected_spans.append((start, min(end, start + room)))
            consumed += min(len(part), room) + separator
        if consumed >= signal_budget:
            break

    indexed_parts = [(0, text[:head_size].strip()), *middle_parts, (len(text) - tail_size, text[-tail_size:].strip())]
    return "\n…\n".join(part for _index, part in sorted(indexed_parts) if part)[:max_chars]


def evidence_quote_present(quote: str, context: str) -> bool:
    # A model occasionally closes a copied sentence fragment with a full stop
    # where the source continues with a comma. Ignore only punctuation at the
    # outer quote boundary; punctuation, negation and word order inside it stay
    # mandatory.
    normalized_quote = _grounding_text(quote).rstrip(".,;:")
    if not (
        _MIN_QUOTE_CHARS <= len(normalized_quote) <= _MAX_QUOTE_CHARS
        and len(_WORD.findall(normalized_quote)) >= _MIN_QUOTE_WORDS
    ):
        return False
    return normalized_quote in _grounding_text(context)


def _application_window_end(quote: str) -> date | None:
    """Return only a date attached to an application closing/window clause."""
    if _NULL_DEADLINE_SIGNAL.search(quote):
        return None
    if not (
        _ABSOLUTE_DEADLINE_ANCHOR_SIGNAL.search(quote)
        or _DEADLINE_SIGNAL.search(quote)
    ):
        return None
    return parse_deadline(quote)


def _has_unexpired_application_window(quotes: list[str], *, today: date) -> bool:
    return any(
        window_end is not None and window_end >= today
        for quote in quotes
        if (window_end := _application_window_end(quote)) is not None
    )


def _dated_intake_end_year(match: re.Match[str]) -> int:
    start_year = int(match.group("start_year"))
    raw_end_year = match.group("end_year")
    if raw_end_year is None:
        return start_year
    end_year = int(raw_end_year)
    if len(raw_end_year) == 2:
        end_year += (start_year // 100) * 100
    return end_year


def _has_current_or_future_dated_intake(text: str, *, today: date) -> bool:
    return any(
        _dated_intake_end_year(match) >= today.year
        for match in _PROGRAMME_DATED_INTAKE_SIGNAL.finditer(text)
    )


def _relative_window_deadline_is_unexpired(quotes: list[str], *, today: date) -> bool:
    text = compact_text(" ".join(quotes))
    relative_days = [
        int(number.group())
        for match in _RELATIVE_WINDOW_DAYS_SIGNAL.finditer(text)
        if (number := re.search(r"\b\d{1,3}\b", match.group("window"))) is not None
    ]
    if not relative_days:
        return False
    publication_dates = [
        candidate
        for quote in quotes
        if _ABSOLUTE_PUBLICATION_ANCHOR_SIGNAL.search(quote)
        for candidate in parse_dates(quote)
    ]
    return bool(
        publication_dates
        and max(publication_dates) + timedelta(days=max(relative_days)) >= today
    )


def _has_explicit_actionable_timing(quotes: list[str], *, today: date) -> bool:
    text = compact_text(" ".join(quotes))
    return bool(
        _CURRENT_APPLICATION_SIGNAL.search(text)
        or _EXPLICIT_FUTURE_APPLICATION_SIGNAL.search(text)
        or _PROGRAMME_RECURRING_DEADLINE_SIGNAL.search(text)
        or _has_unexpired_application_window(quotes, today=today)
        or _has_current_or_future_dated_intake(text, today=today)
        or _relative_window_deadline_is_unexpired(quotes, today=today)
    )


def application_evidence_supports(
    quotes: list[str],
    *,
    actual_vacancy: str,
    open_status: str,
    position_type: str,
    today: date | None = None,
) -> bool:
    """Require the positive quotes, collectively, to support every asserted fact."""
    text = compact_text(" ".join(quotes))
    if actual_vacancy == "yes" and not _VACANCY_SIGNAL.search(text):
        return False
    # ``Application Deadline: None specified`` names a metadata field but does
    # not, by itself, establish that applications are currently accepted.  If
    # the same grounded quotes also contain "Apply now"/"How to apply", that
    # independent signal remains after the null field is removed.
    actionable_application_text = _NULL_DEADLINE_SIGNAL.sub("", text)
    if open_status in {"open", "future"} and not _APPLICATION_SIGNAL.search(actionable_application_text):
        return False
    current_date = today or local_today()
    if open_status == "open" and any(
        _OPEN_NEGATION_SIGNAL.search(quote)
        or (
            _CLOSED_SIGNAL.search(quote)
            and not _CLOSED_CONTRADICTION.search(quote)
        )
        for quote in quotes
    ):
        return False
    if (
        open_status in {"open", "future"}
        and not _has_explicit_actionable_timing(quotes, today=current_date)
    ):
        return False
    if open_status in {"open", "future"} and not relative_application_window_is_anchored(
        quotes,
        today=current_date,
    ):
        return False
    type_signal = _TYPE_SIGNALS.get(position_type)
    return not (actual_vacancy == "yes" and type_signal is not None and not type_signal.search(text))


def relative_application_window_is_anchored(
    quotes: list[str],
    *,
    today: date | None = None,
) -> bool:
    """Reject a relative publication window that cannot establish current openness.

    Calls such as ``within 30 days from publication`` are durable boilerplate:
    without the publication date they may describe an already expired vacancy.
    An explicit current-open marker, an unexpired application deadline or a
    calculable unexpired publication-relative window can support the temporal
    claim; a bare historical publication date cannot.
    """
    text = compact_text(" ".join(quotes))
    if not _RELATIVE_PUBLICATION_WINDOW_SIGNAL.search(text):
        return True
    current_date = today or local_today()
    return bool(
        _CURRENT_APPLICATION_SIGNAL.search(text)
        or _has_unexpired_application_window(quotes, today=current_date)
        or _relative_window_deadline_is_unexpired(quotes, today=current_date)
    )


def opportunity_kind_evidence_supports(
    quotes: list[str],
    opportunity_kind: str,
    *,
    today: date | None = None,
) -> bool:
    """Require evidence for the *shape* of an opportunity, not only its topic.

    This orthogonal label keeps concrete calls and named intakes searchable,
    while routing unsolicited applications to institutions and preventing an
    evergreen "How to apply" page from masquerading as a single vacancy.
    """
    text = compact_text(" ".join(quotes))
    if opportunity_kind == "vacancy":
        return bool(
            _CONCRETE_VACANCY_KIND_SIGNAL.search(text)
            or (
                _SPECIFIC_PROJECT_VACANCY_SIGNAL.search(text)
                and _TYPE_SIGNALS["phd"].search(text)
                and _DEADLINE_SIGNAL.search(text)
            )
        )
    if opportunity_kind == "programme":
        current_date = today or local_today()
        return bool(
            not _SPECIFIC_PROJECT_VACANCY_SIGNAL.search(text)
            and _PROGRAMME_KIND_SIGNAL.search(text)
            and (
                _PROGRAMME_ACTIONABLE_INTAKE_SIGNAL.search(text)
                or _PROGRAMME_RECURRING_DEADLINE_SIGNAL.search(text)
                or _has_current_or_future_dated_intake(text, today=current_date)
                or _has_unexpired_application_window(quotes, today=current_date)
            )
        )
    if opportunity_kind == "spontaneous":
        return bool(_SPONTANEOUS_KIND_SIGNAL.search(text))
    if opportunity_kind == "information":
        return bool(_INFORMATION_KIND_SIGNAL.search(text))
    return False


def classify_opportunity_kind_evidence(
    quotes: list[str],
    *,
    today: date | None = None,
) -> OpportunityKind:
    """Conservative deterministic routing for paths that do not call a judge."""
    for kind in _OPPORTUNITY_KIND_EVIDENCE_ORDER:
        if opportunity_kind_evidence_supports(quotes, kind, today=today):
            return kind
    return UNKNOWN


def negative_evidence_supports(
    quotes: list[str],
    *,
    actual_vacancy: str,
    open_status: str,
    today: date | None = None,
) -> bool:
    """Require negative quotes to support non-vacancy and/or closure labels."""
    text = compact_text(" ".join(quotes))
    if actual_vacancy == "no" and not _NON_VACANCY_SIGNAL.search(text):
        return False
    if open_status != "closed":
        return True
    current_date = today or local_today()
    explicit_closed = any(
        _CLOSED_SIGNAL.search(quote) and not _CLOSED_CONTRADICTION.search(quote)
        for quote in quotes
    )
    dated_closed = any(
        window_end is not None and window_end < current_date
        for quote in quotes
        if (window_end := _application_window_end(quote)) is not None
    )
    return explicit_closed or dated_closed


def explicit_negative_evidence_supports(
    quotes: list[str],
    *,
    actual_vacancy: str,
    open_status: str,
) -> bool:
    """Recognize only high-precision negative facts suitable for deterministic rejection."""
    explicit_closed = open_status == "closed" and any(
        _EXPLICIT_CLOSED_SIGNAL.search(quote) and not _CLOSED_CONTRADICTION.search(quote)
        for quote in quotes
    )
    explicit_non_vacancy = actual_vacancy == "no" and any(
        _EXPLICIT_NON_VACANCY_SIGNAL.search(quote) for quote in quotes
    )
    return explicit_closed or explicit_non_vacancy


def triage_evidence_supports(quotes: list[str], *, decision: str, position_type: str) -> bool:
    """La review rapida può finalizzare solo con prove semanticamente pertinenti."""
    text = compact_text(" ".join(quotes))
    if decision == "eligible":
        type_signal = _TYPE_SIGNALS.get(position_type)
        return bool(
            _VACANCY_SIGNAL.search(text)
            and _APPLICATION_SIGNAL.search(text)
            and (type_signal is None or type_signal.search(text))
        )
    if decision == "rejected":
        return bool(_NON_VACANCY_SIGNAL.search(text) or _CLOSED_SIGNAL.search(text))
    return True
