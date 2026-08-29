"""Normalizza gli item estratti da Crawl4AI nello schema unico Position."""

from __future__ import annotations

import re
import unicodedata
from contextlib import suppress
from datetime import date, datetime
from hashlib import sha256
from urllib.parse import urljoin, urlsplit, urlunsplit

from pydantic import BaseModel

from phd_searcher.position_types import classify_position

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMERIC_RE = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b")
_DAY_MONTH_RE = re.compile(
    r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:\s+(?:de|of)|\.)?\s+"
    r"(?P<month>[^\W\d_]+)\.?(?:\s+(?:de|del|of))?\s+(?P<year>\d(?:\s*\d){3})\b",
    re.IGNORECASE,
)
_MONTH_DAY_RE = re.compile(
    r"\b(?P<month>[^\W\d_]+)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?P<year>\d(?:\s*\d){3})\b",
    re.IGNORECASE,
)
_FORMATS = ("%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%d %B %Y", "%B %d, %Y", "%B %d %Y", "%d %b %Y")
_MONTH_ALIASES: dict[int, tuple[str, ...]] = {
    1: ("jan", "january", "januar", "januari", "janvier", "gennaio", "enero", "janeiro"),
    2: ("feb", "february", "februar", "februari", "fevrier", "febbraio", "febrero", "fevereiro"),
    3: ("mar", "march", "marz", "maart", "mars", "marzo", "marco"),
    4: ("apr", "april", "avril", "aprile", "abril"),
    5: ("may", "mai", "mei", "maggio", "mayo", "maio"),
    6: ("jun", "june", "juni", "juin", "giugno", "junio", "junho"),
    7: ("jul", "july", "juli", "juillet", "luglio", "julio", "julho"),
    8: ("aug", "august", "augustus", "aout", "agosto"),
    9: ("sep", "sept", "september", "septembre", "settembre", "septiembre", "setiembre", "setembro"),
    10: ("oct", "october", "oktober", "octobre", "ottobre", "octubre", "outubro"),
    11: ("nov", "november", "novembre", "noviembre", "novembro"),
    12: ("dec", "december", "dezember", "decembre", "dicembre", "diciembre", "dezembro"),
}
_DEADLINE_CONTEXT_RE = re.compile(
    r"(?:application deadline|submission deadline|registration deadline|closing date|"
    r"application portal.{0,160}\bcloses?|apply(?:ing)?\s+(?:no later than|by)|open (?:until|till)|"
    r"applications?\s+(?:close|until|between)|scadenza|termine.{0,40}(?:domand|candidatur)|"
    r"bewerbungsfrist|bewerbung(?:en)?.{0,160}\bbis(?:\s+zum)?|"
    r"date limite|fecha límite|plazo de presentación|"
    r"formalización de solicitudes|solicitar.{0,160}(?:entre|desde|del|hasta)).{0,500}",
    re.IGNORECASE | re.DOTALL,
)
_DEADLINE_RANGE_RE = re.compile(
    r"\b(?:between|from|desde|entre|del)\b.{0,180}\b(?:and|to|until|through|hasta|al|a)\b|"
    r"\bapplication\s+portal\b.{0,180}\bopens?\b.{0,180}\bcloses?\b|"
    r"\bdeadlines\b.{0,180}\b(?:and|or)\b",
    re.IGNORECASE,
)
_NULL_DEADLINE_RE = re.compile(
    r"\b(?:application\s+deadline|closing\s+date|deadline)\b"
    r"(?:\s|[*:\u2013\u2014_-]){0,16}"
    r"(?:none(?:\s+specified)?|not\s+specified|n\s*/?\s*a|no\s+deadline)\b",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"(?<!\w)\d[\d\s.,]*(?!\w)")
_ECB_CURRENCY_CODES = (
    "EUR",
    "USD",
    "JPY",
    "BGN",
    "CZK",
    "DKK",
    "GBP",
    "HUF",
    "PLN",
    "RON",
    "SEK",
    "CHF",
    "ISK",
    "NOK",
    "TRY",
    "AUD",
    "BRL",
    "CAD",
    "CNY",
    "HKD",
    "IDR",
    "ILS",
    "INR",
    "KRW",
    "MXN",
    "MYR",
    "NZD",
    "PHP",
    "SGD",
    "THB",
    "ZAR",
)
# Prefer an explicit code over an ambiguous symbol (for example ``CAD $``).
_CURRENCIES = {
    **{code: code for code in _ECB_CURRENCY_CODES},
    "€": "EUR",
    "£": "GBP",
    "$": "USD",
}
_PERIODS = {
    "hour": ("hour", "hourly", "/h", "per hour", "ora", "orario"),
    "week": ("week", "weekly", "per week", "settimana", "settimanale"),
    "month": ("month", "monthly", "/month", "per month", "mese", "mensile"),
    "year": ("year", "annual", "annually", "/year", "per year", "anno", "annuo", "annuale"),
}
_COMPENSATION_LINE_RE = re.compile(
    r"(?im)^.*(?:salary|stipend|compensation|remuneration|gross|net salary|€|\bEUR\b|\bUSD\b|\bGBP\b|\bCHF\b).*$"
)
_LABELLED_COMPENSATION_RE = re.compile(
    r"(?im)^(?P<label>salary|stipend|compensation|remuneration|pay(?:\s+range)?)"
    r"\s*:?\s*\n(?:[ \t]*\n)*[ \t]*(?P<value>[^\n]{1,500})"
)
_COMPENSATION_SIGNAL_RE = re.compile(
    r"\b(?:salary|stipend|compensation|remuneration|pay(?:ment|grade)?|wages?|gross|net|"
    r"scholarships?|allowances?|benefits? package|financial conditions|"
    r"vergütung|gehalt|salaire|retribu(?:zione|tion)|compenso|remunerazione|"
    r"borsa|assegno|wynagrodzenie)\b|"
    + "|".join(re.escape(marker) for marker in _CURRENCIES),
    re.IGNORECASE,
)
_DURATION_LINE_RE = re.compile(
    r"(?im)^.*(?:duration.{0,80}\b\d+\s*(?:months?|years?)|"
    r"(?:contract|appointment|funding period).{0,80}\b\d+\s*(?:months?|years?)|"
    r"\b\d+\s*months?\b.{0,40}(?:contract|position|funding)).*$"
)
_CONTRACT_DURATION_RE = re.compile(
    r"\b(?:contract(?:\s+type)?|appointment(?:\s+type)?|term\s+of\s+employment)\b"
    r"\s*(?:[:\-\u2013\u2014]|\r?\n)*\s*"
    r"(?P<value>permanent|open[- ]ended|indefinite|"
    r"fixed[- ]term(?:\s+(?:for|of)\s+\d+\s*(?:months?|years?))?|"
    r"temporary(?:\s+(?:for|of)\s+\d+\s*(?:months?|years?))?)\b",
    re.IGNORECASE,
)
_PUBLISHED_LINE_RE = re.compile(r"(?im)^.*(?:posted on|publication date|published on|data di pubblicazione).*$")
_RESEARCH_GROUP_LINE_RE = re.compile(
    r"(?im)^(?:research group|research team|laboratory|lab|department|unit)\s*[:\-]\s*(.{2,500})$"
)


class NormalizedPosition(BaseModel):
    title: str
    url: str
    description: str = ""
    area: str | None = None
    language: str | None = None
    duration_raw: str | None = None
    compensation_raw: str | None = None
    compensation_min: float | None = None
    compensation_max: float | None = None
    compensation_currency: str | None = None
    compensation_period: str | None = None
    published_raw: str | None = None
    published_at: date | None = None
    position_type: str = "other"
    research_group: str | None = None
    deadline_raw: str | None = None
    deadline: date | None = None


def _month_key(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value).casefold()
        if not unicodedata.combining(char)
    ).rstrip(".")


_MONTH_NUMBERS = {
    _month_key(alias): number
    for number, aliases in _MONTH_ALIASES.items()
    for alias in aliases
}


def parse_dates(raw: str | None) -> list[date]:
    """Extract every unambiguous calendar date used by source metadata."""
    if not raw:
        return []
    text = raw.strip()
    parsed: set[date] = set()
    for match in _ISO_RE.finditer(text):
        with suppress(ValueError):
            parsed.add(date.fromisoformat(match.group()))
    for match in _NUMERIC_RE.finditer(text):
        candidate = match.group()
        for fmt in _FORMATS:
            try:
                parsed.add(datetime.strptime(candidate, fmt).date())
                break
            except ValueError:
                continue
    for regex in (_DAY_MONTH_RE, _MONTH_DAY_RE):
        for match in regex.finditer(text):
            month = _MONTH_NUMBERS.get(_month_key(match.group("month")))
            if month is None:
                continue
            try:
                # PDF text extraction may split a visually continuous year
                # (``202 6`` or ``2 0 2 6``). The date grammar itself keeps
                # this repair scoped to a day-month-year expression.
                year = int(re.sub(r"\s+", "", match.group("year")))
                parsed.add(date(year, month, int(match.group("day"))))
            except ValueError:
                continue
    return sorted(parsed)


def parse_deadline(raw: str | None) -> date | None:
    """Return the closing/end date when a source exposes a date range."""
    # A metadata row such as ``Application Deadline: None specified | Start
    # Date: 21 September`` contains a date, but explicitly says it is not the
    # application deadline.  Never borrow a neighbouring start/project date.
    if raw and _NULL_DEADLINE_RE.search(raw):
        return None
    dates = parse_dates(raw)
    return max(dates, default=None)


def extract_deadline(text: str) -> tuple[str | None, date | None]:
    """Find a dated application/closing clause in a fetched detail document.

    The lexical gate avoids treating publication, event or project dates as an
    application deadline. Within a genuine application range, the last date is
    the closing date.
    """
    compact = re.sub(r"\s+", " ", text).strip()
    candidates: list[tuple[date, str]] = []
    for match in _DEADLINE_CONTEXT_RE.finditer(compact):
        snippet = match.group().strip()
        if _NULL_DEADLINE_RE.search(snippet):
            continue
        dates = parse_dates(snippet)
        if not dates:
            continue
        # A generous context is useful for long clauses, but it can also
        # contain a later interview/start date. Use the first chronological
        # date for a single deadline and the last only for an explicit range
        # or recurring set of deadlines.
        deadline = max(dates) if _DEADLINE_RANGE_RE.search(snippet) else min(dates)
        candidates.append((deadline, snippet))
    if not candidates:
        return None, None
    deadline, snippet = max(candidates, key=lambda item: item[0])
    return snippet[:256], deadline


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _position_url(title: str, href: str, base_url: str) -> str:
    """Usa il dettaglio reale o un URL stabile della listing quando il bando non ha un link.

    Alcune pagine pubblicano più vacancy direttamente nello stesso documento. In quel
    caso un frammento derivato dal titolo evita collisioni UNIQUE senza inventare una
    pagina esterna. Un link alla sola homepage, estratto da menu/breadcrumb, non è un
    dettaglio valido quando la listing si trova su un percorso più specifico.
    """
    resolved = urljoin(base_url, href) if href else ""
    base = urlsplit(base_url)
    target = urlsplit(resolved) if resolved else None
    base_segments = [segment for segment in base.path.split("/") if segment]
    target_segments = [segment for segment in target.path.split("/") if segment] if target else []
    homepage_link = bool(
        target
        and target.scheme == base.scheme
        and target.netloc == base.netloc
        and len(target_segments) <= 1
        and len(base_segments) > len(target_segments)
    )
    if resolved and not homepage_link:
        return resolved
    return synthetic_position_url(title, base_url)


def synthetic_position_url(title: str, base_url: str) -> str:
    """Stable fallback identity used only when a listing exposes no detail URL."""
    base = urlsplit(base_url)
    fragment = f"position-{sha256(title.casefold().encode()).hexdigest()[:16]}"
    return urlunsplit((base.scheme, base.netloc, base.path, base.query, fragment))


def _number(raw: str) -> float | None:
    value = raw.replace(" ", "").strip(".,")
    if not value:
        return None
    if "," in value and "." in value:
        decimal = "," if value.rfind(",") > value.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        value = value.replace(thousands, "").replace(decimal, ".")
    elif "," in value or "." in value:
        separator = "," if "," in value else "."
        parts = value.split(separator)
        value = "".join(parts) if all(len(part) == 3 for part in parts[1:]) else ".".join(parts)
    try:
        return float(value)
    except ValueError:
        return None


def parse_compensation(raw: str | None) -> tuple[float | None, float | None, str | None, str | None]:
    """Estrae intervallo, valuta e periodo conservando sempre anche il testo originale."""
    if not raw:
        return None, None, None, None
    amounts = [value for match in _AMOUNT_RE.findall(raw) if (value := _number(match)) is not None]
    folded = raw.casefold()
    currency = next((code for marker, code in _CURRENCIES.items() if marker.casefold() in folded), None)
    period = next((name for name, markers in _PERIODS.items() if any(marker in folded for marker in markers)), None)
    return (
        min(amounts) if amounts else None,
        max(amounts) if amounts else None,
        currency,
        period,
    )


def _plausible_compensation(raw: str | None) -> bool:
    """Reject schema spillover while retaining textual salary/benefit clauses."""
    return bool(raw and _COMPENSATION_SIGNAL_RE.search(raw))


def extract_terms(text: str) -> tuple[str | None, str | None, str | None]:
    """Trova righe candidate nel dettaglio completo, senza inventare valori assenti."""
    compensation = _COMPENSATION_LINE_RE.search(text)
    labelled_compensation = _LABELLED_COMPENSATION_RE.search(text)
    compensation_text = compensation.group().strip()[:512] if compensation else None
    if labelled_compensation:
        candidate = (
            f"{labelled_compensation.group('label')}: "
            f"{labelled_compensation.group('value').strip()}"
        )[:512]
        if _plausible_compensation(candidate):
            compensation_text = candidate
    duration = _DURATION_LINE_RE.search(text)
    contract = _CONTRACT_DURATION_RE.search(text) if duration is None else None
    published = _PUBLISHED_LINE_RE.search(text)
    return (
        compensation_text,
        (
            duration.group().strip()[:256]
            if duration
            else f"Contract: {contract.group('value').strip()}"[:256]
            if contract
            else None
        ),
        published.group().strip()[:256] if published else None,
    )


def extract_research_group(text: str) -> str | None:
    match = _RESEARCH_GROUP_LINE_RE.search(text)
    return match.group(1).strip()[:512] if match else None


def normalize_item(item: dict[str, object], *, base_url: str) -> NormalizedPosition | None:
    """Un item estratto -> NormalizedPosition; None se manca il titolo."""
    title = _text(item.get("title"))
    href = _text(item.get("url"))
    if not title:
        return None
    deadline_raw = _text(item.get("deadline"))[:256] or None
    published_raw = _text(item.get("published"))[:256] or None
    compensation_raw = _text(item.get("compensation"))[:512] or None
    compensation_min, compensation_max, compensation_currency, compensation_period = parse_compensation(compensation_raw)
    if not _plausible_compensation(compensation_raw):
        compensation_raw = None
        compensation_min = compensation_max = None
        compensation_currency = compensation_period = None
    description = _text(item.get("description"))
    return NormalizedPosition(
        title=title,
        url=_position_url(title, href, base_url),
        description=description,
        area=_text(item.get("area"))[:256] or None,
        language=_text(item.get("language"))[:8] or None,
        duration_raw=_text(item.get("duration"))[:256] or None,
        compensation_raw=compensation_raw,
        compensation_min=compensation_min,
        compensation_max=compensation_max,
        compensation_currency=compensation_currency,
        compensation_period=compensation_period,
        published_raw=published_raw,
        published_at=parse_deadline(published_raw),
        position_type=classify_position(title, description, _text(item.get("position_type")) or None),
        research_group=_text(item.get("research_group"))[:512] or None,
        deadline_raw=deadline_raw,
        deadline=parse_deadline(deadline_raw),
    )
