"""Tassonomia stabile e classificazione deterministica delle opportunità."""

from __future__ import annotations

import re

POSITION_TYPES: dict[str, str] = {
    "phd": "PhD / doctoral / predoctoral position",
    "masters_mph": "Master / MPH",
    "medical_doctorate": "Medical doctorate",
    "internship": "Internship / traineeship",
    "assistantship": "Research / teaching assistantship",
    "research_fellowship": "Research fellowship / grant",
    "postdoc": "Postdoc",
    "research_staff": "Research position",
    "faculty": "Faculty / lecturer",
    "other": "Other opportunity",
}

# A singular, subject-qualified doctoral offer is more specific than its
# funding mechanism. Do not match generic plural funding directories, travel
# grants for PhD students, or mentions buried in shared description text.
_DOCTORAL_FELLOWSHIP_TITLE = re.compile(
    r"^(?:(?:fully[ -])?funded\s+)?(?:Ph\.?D\.?|(?:pre[- ]?)?doctoral)\s+"
    r"(?:fellowship|scholarship|studentship)\s+(?:in|on|at|within)\s+\S",
    re.I,
)

# A concrete job title must not become a PhD/fellowship merely because its
# qualifications, benefits or neighbouring programmes mention one. Keep the
# existing body fallback for generic/multilingual titles not covered here.
_NAMED_JOB_TITLE = re.compile(
    r"\b(?:developer|officer|specialist|engineer|curator|bioinformatician|"
    r"team leader|coordinator|manager|administrator|designer|analyst|scientist)\b",
    re.I,
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("postdoc", re.compile(r"\bpost[ -]?doc(?:toral)?\b|\bpostdottor", re.I)),
    (
        "internship",
        re.compile(
            r"\bintern(?:ship)?\b|\btraineeship\b|\bresearch trainee\b|"
            r"\btirocin(?:io|ante)\b|\boffre de stage\b|\bstagiaire\b|"
            r"\bstage (?:de|en|di|in) (?:recherche|ricerca|research|laboratoire|laboratory)\b|"
            r"\bpraktik(?:um|ant(?:in)?)\b|\bstageplaats\b|\bonderzoeksstage\b|"
            r"\b(?:prácticas?|pasantía) de investigación\b|\bestágio de (?:investigação|pesquisa)\b",
            re.I,
        ),
    ),
    (
        "assistantship",
        re.compile(
            r"\b(?:graduate|research|teaching) assistant(?:ship)?\b|"
            r"\b(?:RA|TA) position\b|"
            r"\b(?:studentische|wissenschaftliche|stud\.?)\s+hilfskr(?:aft|äfte)\b",
            re.I,
        ),
    ),
    (
        "research_fellowship",
        re.compile(
            r"\bresearch (?:fellowships?|fellows?|grants?)\b|\bfellowships?\b|\bstudentships?\b|"
            r"\bscholarships?\b|\b(?:awards?|prizes?)\b|\bpremios?\b|"
            r"\b(?:travel|conference) grants?\b|\bbecas?\b(?!\s+predoctoral)|\bayudas?\b|"
            r"assegn[oi] di ricerca|bors[ae] di ricerca",
            re.I,
        ),
    ),
    ("masters_mph", re.compile(r"\bMPH\b|master(?:'s)? (?:degree|programme|program|position)", re.I)),
    ("medical_doctorate", re.compile(r"\bMD[ -]?PhD\b|medical doctorate|doctor of medicine", re.I)),
    (
        "phd",
        re.compile(
            r"\bPh\.?D\.?\b|pre[- ]?doctoral|doctoral|doctorate|dottorat|doktorand|doctorat|doctorado|doutoramento|promovend",
            re.I,
        ),
    ),
    (
        "faculty",
        re.compile(
            r"\bassistant professor\b|\bassociate professor\b|\bprofessor\b|\blecturer\b|"
            r"\b(?:academic|programme?|program) director\b|\bfaculty positions?\b|"
            r"\bcontratt[oi](?: integrativ[oi])? di insegnament[oi]\b|"
            r"\bincaric(?:o|hi) di insegnamento\b|"
            r"\bcarichi? di didattica\b",
            re.I,
        ),
    ),
    (
        "research_staff",
        re.compile(
            r"\bresearcher\b|\bresearch scientist\b|\bresearch engineer\b|\bresearch associate\b|"
            r"\bscientific officer\b|"
            r"\b(?:user\s+)?research officer\b|\bbioinformatician\b|"
            r"\b(?:biological|scientific|genomic) curator\b|"
            r"\b(?:genomics|bioinformatics|computational biology)"
            r"(?:\s+[A-Za-z-]+){0,5}\s+team (?:leader|lead)\b|"
            r"\bricercatore\b|\bincaric(?:o|hi) di ricerca\b|\bincaric(?:o|hi) di lavoro autonomo\b",
            re.I,
        ),
    ),
)


def classify_position(title: str, description: str = "", explicit: str | None = None) -> str:
    """Classifica senza LLM; una categoria esplicita valida ha precedenza."""
    if explicit in POSITION_TYPES and explicit != "other":
        return explicit
    # Il titolo è più affidabile del testo pagina, che spesso include menu e
    # descrizioni di corsi non collegati al tipo di contratto della vacancy.
    title_kind = next((kind for kind, pattern in _PATTERNS if pattern.search(title)), None)
    if title_kind == "research_fellowship" and _DOCTORAL_FELLOWSHIP_TITLE.match(title.strip()):
        return "phd"
    if title_kind:
        return title_kind
    if _NAMED_JOB_TITLE.search(title):
        return "other"
    return next((kind for kind, pattern in _PATTERNS if pattern.search(description)), "other")
