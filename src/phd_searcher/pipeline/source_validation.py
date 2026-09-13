"""Cheap source checks before schema/model work. No verdict about individual jobs."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

from bs4 import BeautifulSoup

_ERROR_HEADINGS = {
    "not found",
    "page not found",
    "404",
    "404 not found",
    "404 page not found",
    "page introuvable",
    "seite nicht gefunden",
}
_NO_OPENINGS = re.compile(
    r"^(?:at the moment[, ]+)?(?:there are|we have)\s+(?:currently\s+)?no\s+"
    r"(?:open\s+)?(?:job openings|job vacancies|open positions|vacancies|positions)"
    r"\s*[.!](?:\s|$)",
    re.I,
)


def _fold(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _json_objects(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _json_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _json_objects(nested)


def employer_evidence(html: str, institution: str) -> bool:
    """External ATS allowed when structured JobPosting names the exact employer.

    An arbitrary mention/link or an Organization unrelated to hiring isn't proof.
    Absent evidence defers the source, it does not reject the institution/jobs.
    """
    soup = BeautifulSoup(html, "html.parser")
    matched = False
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.get_text())
        except ValueError:
            continue
        for obj in _json_objects(payload):
            types = obj.get("@type", [])
            types = [types] if isinstance(types, str) else types
            employer = obj.get("hiringOrganization")
            if isinstance(types, list) and "JobPosting" in types:
                if (
                    not isinstance(employer, dict)
                    or not _fold(institution)
                    or _fold(str(employer.get("name", ""))) != _fold(institution)
                ):
                    return False  # Mixed-employer aggregators are not this institute's board.
                matched = True
    return matched


def page_state(html: str, status: int | None = None) -> str | None:
    if status in {404, 410}:
        return "unavailable"
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup.find(id="content") or soup
    heading = main.find("h1")
    if heading is not None and _fold(heading.get_text(" ", strip=True)) in _ERROR_HEADINGS:
        return "unavailable"
    # Contradictory positive evidence means abstain, not hide a mixed page.
    if '"JobPosting"' in html or any(
        re.search(r"\b(phd|postdoctoral|doctoral|researcher|professor)\b", a.get_text(" ", strip=True), re.I)
        for a in main.find_all("a", href=True)
    ):
        return None
    for block in main.find_all(["p", "h1", "h2"], limit=15):
        text = " ".join(block.get_text(" ", strip=True).split())
        if _NO_OPENINGS.search(text):
            return "empty"
    return None


class SchemaDeferredError(Exception):
    """A recorded preflight disposition, not an LLM/transport failure."""
