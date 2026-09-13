"""Structured adapters for official job boards that HTML extraction cannot see.

Adapter configuration normally lives inside ``ListingPage.extraction_schema``.
The two audited Copenhagen table URLs also have a deterministic raw-HTML path
so existing schemas benefit without a database rewrite. Both paths use the
normal scrape checkpoints/retry logic.
Adapters return the same raw item contract as Crawl4AI; normalization and
deduplication therefore remain centralized in :mod:`pipeline.normalize`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import cast
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from phd_searcher.pipeline.departmental_sources import DEPARTMENTAL_URLS, departmental_items
from phd_searcher.pipeline.normalize import extract_terms, parse_compensation
from phd_searcher.pipeline.workday import fetch_workday_page

_TALENTLINK = "talentlink"
_TALENTADORE = "talentadore"
_COPENHAGEN_LISTINGS = frozenset(
    {
        "https://employment.ku.dk/phd/",
        "https://employment.ku.dk/all-vacancies/",
    }
)
SUPPORTED_SOURCE_ADAPTERS = frozenset({_TALENTLINK, _TALENTADORE, "departmental", "workday"})
_ALLOWED_ADAPTER_HOSTS: dict[str, frozenset[str]] = {
    _TALENTLINK: frozenset({"recruitmentplatform.com"}),
    _TALENTADORE: frozenset({"ats.talentadore.com"}),
}
_MIN_ACCEPTED_RATIO = 0.9
_TALENTLINK_SALARY_RANGE = re.compile(r"^\s*(?P<minimum>\d{4,})\s+(?P<maximum>\d{4,})(?P<suffix>\s+\D.*)?$")


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag in {"br", "p", "div", "h1", "h2", "h3", "h4", "li"}:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "h1", "h2", "h3", "h4", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    parser = _PlainTextParser()
    parser.feed(value)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def _schema_string(schema: dict[str, object], key: str) -> str:
    value = schema.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"source adapter requires non-empty {key}")
    return value.strip()


def _adapter_url(schema: dict[str, object], key: str, adapter: str) -> str:
    value = _schema_string(schema, key)
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    allowed = _ALLOWED_ADAPTER_HOSTS[adapter]
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not hostname
        or not any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed)
    ):
        raise RuntimeError(f"untrusted {adapter} {key} URL")
    return value


def _jobs(payload: object, adapter: str) -> list[object]:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise RuntimeError(f"{adapter} response has no jobs list")
    return cast(list[object], payload["jobs"])


def _validate_acceptance(adapter: str, raw_count: int, accepted_count: int) -> None:
    if raw_count and accepted_count / raw_count < _MIN_ACCEPTED_RATIO:
        raise RuntimeError(f"{adapter} response shape changed: accepted {accepted_count}/{raw_count} jobs")


def _talentlink_salary(value: object) -> str:
    """Restore the range separator stripped by TalentLink's public feed."""

    raw = str(value or "").strip()
    match = _TALENTLINK_SALARY_RANGE.fullmatch(raw)
    if match is None:
        return raw
    suffix = match.group("suffix") or ""
    return f"{match.group('minimum')} - {match.group('maximum')}{suffix}"


def source_adapter_name(schema: dict[str, object] | None) -> str | None:
    value = (schema or {}).get("adapter")
    if value is None:
        return None
    if not isinstance(value, str) or value not in SUPPORTED_SOURCE_ADAPTERS:
        raise RuntimeError(f"unsupported source adapter: {value!r}")
    return value


def normalize_source_item_formats(
    items: list[dict[str, object]],
    schema: dict[str, object] | None,
) -> list[dict[str, object]]:
    """Apply explicit source-owned date formats before generic normalization.

    Numeric dates are inherently ambiguous. A source declaration is safer
    than guessing globally from values such as ``9/11/2026``.
    """

    raw_formats = (schema or {}).get("dateFormats")
    if not isinstance(raw_formats, dict):
        return items
    formats = {key: value for key, value in raw_formats.items() if isinstance(key, str) and isinstance(value, str)}
    normalized: list[dict[str, object]] = []
    for item in items:
        updated = dict(item)
        for field, date_format in formats.items():
            raw_value = updated.get(field)
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            token = raw_value.strip().split(maxsplit=1)[0]
            try:
                updated[f"__phdbot_{field}_date"] = datetime.strptime(token, date_format).date().isoformat()
            except ValueError:
                # Preserve the evidence for the generic multilingual parser.
                continue
        normalized.append(updated)
    return normalized


def _epoch_milliseconds_date(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def talentlink_items(
    payload: object,
    schema: dict[str, object],
) -> list[dict[str, object]]:
    """Map one TalentLink guest API response to PHDBOT raw items."""

    raw_jobs = _jobs(payload, "TalentLink")
    language = str(schema.get("language") or "en_GB")
    tech_id = _schema_string(schema, "siteTechId")
    api_host = _schema_string(schema, "apiHost").rstrip("/")
    salary_field = str(schema.get("salaryField") or "SLOVLIST74")
    department_fields = schema.get("departmentFields")
    if not isinstance(department_fields, list):
        department_fields = ["SLOVLIST76", "SLOVLIST72", "SLOVLIST20"]

    items: list[dict[str, object]] = []
    for raw_job in raw_jobs:
        if not isinstance(raw_job, dict):
            continue
        fields = raw_job.get("jobFields")
        if not isinstance(fields, dict):
            continue
        title = str(fields.get("SJOBTITLE") or fields.get("jobTitle") or "").strip()
        job_id = fields.get("id") or raw_job.get("id")
        if not title or job_id in (None, ""):
            continue
        url = str(fields.get("applicationUrl") or "").strip()
        if not url:
            url = f"{api_host}/apply-app/pages/application-form?jobId={tech_id}-{job_id}&langCode={language}"
        else:
            url = urljoin(f"{api_host}/", url)
        sections: list[str] = []
        custom_fields = raw_job.get("customFields")
        if isinstance(custom_fields, list):
            for section in custom_fields:
                if not isinstance(section, dict):
                    continue
                heading = str(section.get("title") or "").strip()
                content = _plain_text(section.get("content"))
                if content:
                    sections.append(f"{heading}\n{content}" if heading else content)
        description = "\n\n".join(sections)
        inferred_compensation, inferred_duration, _ = extract_terms(description)
        department_values = [
            str(fields.get(key) or "").strip() for key in department_fields if isinstance(key, str) and fields.get(key)
        ]
        compensation = _talentlink_salary(fields.get(salary_field)) or inferred_compensation
        salary_currency = str(schema.get("salaryCurrency") or "").strip().upper()
        parsed_currency = parse_compensation(compensation)[2]
        if (
            compensation
            and salary_currency
            and parsed_currency is None
            and any(char.isdigit() for char in compensation)
        ):
            compensation = f"{salary_currency} {compensation}"
        items.append(
            {
                "title": title,
                "url": url,
                "description": description,
                "area": " · ".join(dict.fromkeys(department_values)),
                "research_group": department_values[0] if department_values else "",
                "deadline": _epoch_milliseconds_date(fields.get("DPOSTINGEND")) or "",
                "published": _epoch_milliseconds_date(fields.get("DPOSTINGSTART")) or "",
                "compensation": compensation or "",
                "duration": inferred_duration or "",
                "language": language.split("_", 1)[0],
            }
        )
    _validate_acceptance("TalentLink", len(raw_jobs), len(items))
    return items


def talentadore_items(payload: object, *, base_url: str = "") -> list[dict[str, object]]:
    """Map an official TalentAdore public feed to PHDBOT raw items."""

    raw_jobs = _jobs(payload, "TalentAdore")
    items: list[dict[str, object]] = []
    for raw_job in raw_jobs:
        if not isinstance(raw_job, dict):
            continue
        title = str(raw_job.get("name") or "").strip()
        url = str(raw_job.get("link") or "").strip()
        if not title or not url:
            continue
        if base_url:
            url = urljoin(base_url, url)
        description = str(
            raw_job.get("description_text_en")
            or raw_job.get("description_text")
            or _plain_text(raw_job.get("description_html"))
            or ""
        ).strip()
        compensation, duration, _ = extract_terms(description)
        unit = str(raw_job.get("business_unit_name") or "").strip()
        items.append(
            {
                "title": title,
                "url": url,
                "description": description,
                "area": unit,
                "research_group": unit,
                "deadline": str(raw_job.get("due_date") or ""),
                "published": str(raw_job.get("published_at") or raw_job.get("start_date") or ""),
                "compensation": compensation or "",
                "duration": duration or "",
                "language": "en",
            }
        )
    _validate_acceptance("TalentAdore", len(raw_jobs), len(items))
    return items


def copenhagen_items(html: str, source_url: str) -> list[dict[str, object]]:
    """Read the complete server table before DataTables detaches hidden pages.

    A missing/changed table is a failure, not an empty successful refresh that
    could age out existing jobs. Only the two audited official listings use
    this path; no generic JavaScript or table heuristics are introduced.
    """
    if source_url not in _COPENHAGEN_LISTINGS:
        raise RuntimeError("untrusted Copenhagen listing URL")
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.vacancies")
    if table is None:
        raise RuntimeError("Copenhagen vacancy table missing")
    headers = [cell.get_text(" ", strip=True).casefold() for cell in table.select("thead th")]
    if headers != ["title", "faculty", "location", "deadline"]:
        raise RuntimeError("Copenhagen vacancy table columns changed")
    items: list[dict[str, object]] = []
    rows = table.select("tbody tr")
    for row in rows:
        cells = row.find_all("td", recursive=False)
        link = cells[0].find("a", href=True) if len(cells) == 4 else None
        if link is None:
            raise RuntimeError("Copenhagen malformed vacancy row")
        title = link.get_text(" ", strip=True)
        url = urljoin(source_url, str(link.get("href")))
        parsed = urlsplit(url)
        if (
            not title
            or parsed.scheme != "https"
            or parsed.netloc != "employment.ku.dk"
            or not re.fullmatch(r"show=\d+", parsed.query)
        ):
            raise RuntimeError("Copenhagen invalid vacancy link")
        deadline = cells[3].get_text(" ", strip=True)
        try:
            normalized_deadline = datetime.strptime(deadline, "%d-%m-%Y").date().isoformat()
        except ValueError as exc:
            raise RuntimeError("Copenhagen invalid deadline") from exc
        items.append(
            {
                "title": title,
                "url": url,
                "area": cells[1].get_text(" ", strip=True),
                "research_group": cells[2].get_text(" ", strip=True),
                "deadline": deadline,
                "__phdbot_deadline_date": normalized_deadline,
            }
        )
    return items


async def fetch_source_adapter(
    schema: dict[str, object],
    *,
    page_number: int,
    source_url: str | None = None,
) -> list[dict[str, object]] | None:
    """Fetch one durable scrape page, or ``None`` for ordinary HTML sources."""

    adapter = source_adapter_name(schema)
    if adapter == "workday":
        if source_url is None:
            raise RuntimeError("Workday requires its admitted source URL")
        return await fetch_workday_page(source_url, page_number)
    if adapter == "departmental":
        if source_url not in DEPARTMENTAL_URLS:
            raise RuntimeError("untrusted departmental source URL")
        if page_number > 0:
            return []
        assert source_url is not None
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            response = await client.get(source_url)
            response.raise_for_status()
            return departmental_items(response.text, source_url)
    if adapter is None and source_url in _COPENHAGEN_LISTINGS:
        if page_number > 0:
            return []  # All client-side pages are in the first HTML response.
        assert source_url is not None
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            response = await client.get(source_url)
            response.raise_for_status()
            return copenhagen_items(response.text, source_url)
    if adapter is None:
        return None
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        if adapter == _TALENTLINK:
            host = _adapter_url(schema, "apiHost", adapter).rstrip("/")
            tech_id = _schema_string(schema, "siteTechId")
            language = str(schema.get("language") or "en_GB")
            raw_page_size = schema.get("pageSize", 100)
            page_size = int(raw_page_size) if isinstance(raw_page_size, int | str) else 100
            page_size = max(1, min(page_size, 500))
            response = await client.post(
                f"{host}/fo/rest/jobs",
                params={
                    "firstResult": page_number * page_size,
                    "maxResults": page_size,
                    "sortBy": "SJOBTITLE",
                    "sortOrder": "asc",
                },
                headers={
                    "Accept": "application/json",
                    "username": f"{tech_id}:guest:FO",
                    "password": "guest",
                    "lumesse-language": language,
                },
                json={"searchCriteria": {"criteria": []}},
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                globals_payload = payload.get("globals")
                raw_jobs = payload.get("jobs")
                # TalentLink omits the key entirely beyond the final page and
                # reports ``jobsCount: 0``. This is its canonical empty-page
                # response, not a malformed payload.
                if (
                    not isinstance(raw_jobs, list)
                    and isinstance(globals_payload, dict)
                    and globals_payload.get("jobsCount") == 0
                ):
                    return []
                if isinstance(globals_payload, dict) and isinstance(raw_jobs, list):
                    raw_total = globals_payload.get("jobsCount")
                    try:
                        total = int(raw_total)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        total = -1
                    if total >= 0:
                        offset = page_number * page_size
                        expected = min(page_size, max(total - offset, 0))
                        if len(raw_jobs) < expected:
                            raise RuntimeError(
                                "TalentLink response is incomplete: "
                                f"received {len(raw_jobs)}/{expected} jobs for page {page_number}"
                            )
            return talentlink_items(payload, schema)
        feed_url = _adapter_url(schema, "feedUrl", adapter)
        response = await client.get(feed_url, headers={"Accept": "application/json"})
        response.raise_for_status()
        return talentadore_items(response.json(), base_url=feed_url) if page_number == 0 else []
