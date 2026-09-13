"""Public Workday boards: preserve the publisher's scope, fetch bounded pages.

No tenant/institution registry. Admission requires a current explicit link on
the institution's own recruitment page; a hostname alone is never ownership.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from phd_searcher.pipeline.normalize import extract_terms

_HOST = re.compile(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com")
_PATH = re.compile(r"/([a-z]{2}-[A-Z]{2})/([A-Za-z0-9_-]+)/?")
_FACETS = frozenset({"locations", "locationCountry", "jobFamilyGroup", "jobFamily", "timeType", "workerSubType"})
_JOBS = re.compile(r"\b(jobs?|vacancies|openings|positions|stellenangebote)\b", re.I)
_RECRUITMENT = re.compile(r"(?:^|/)(?:jobs?|careers?|vacancies|recruitment|stellenangebote)(?:/|$)", re.I)
PAGE_SIZE = 20


@dataclass(frozen=True)
class WorkdayBoard:
    origin: str
    tenant: str
    locale: str
    site: str
    facets: dict[str, list[str]]

    @property
    def api(self) -> str:
        return f"{self.origin}/wday/cxs/{self.tenant}/{self.site}"

    @property
    def public(self) -> str:
        return f"{self.origin}/{self.locale}/{self.site}"


def workday_board(url: str) -> WorkdayBoard | None:
    try:
        p = urlsplit(url)
        host = _HOST.fullmatch(p.hostname or "")
        path = _PATH.fullmatch(p.path)
        facets = parse_qs(p.query, keep_blank_values=True, strict_parsing=True)
        if (
            p.scheme != "https"
            or p.username
            or p.password
            or p.port not in {None, 443}
            or p.fragment
            or not host
            or not path
            or set(facets) - _FACETS
            or any(not re.fullmatch(r"[a-zA-Z0-9_-]+", v) for values in facets.values() for v in values)
        ):
            return None
        return WorkdayBoard(f"https://{p.hostname}", host[1], path[1], path[2], facets)
    except ValueError:
        return None


def recruitment_referrer(referrer: str, website: str) -> bool:
    """Only official recruitment pages, not links found on external hubs."""
    p, w = urlsplit(referrer), urlsplit(website)
    host = (p.hostname or "").removeprefix("www.")
    official = (w.hostname or "").removeprefix("www.")
    return bool(
        p.scheme in {"http", "https"}
        and not p.username
        and not p.password
        and host
        and official
        and (host == official or host.endswith(f".{official}"))
        and _RECRUITMENT.search(p.path)
    )


def linked_workday_board(html: str, referrer: str, target: str) -> bool:
    board = workday_board(target)
    if board is None:
        return False
    soup = BeautifulSoup(html, "html.parser")
    return any(
        _JOBS.search(a.get_text(" ", strip=True)) and workday_board(urljoin(referrer, str(a.get("href", "")))) == board
        for a in soup.select("a[href]")
    )


async def fetch_workday_page(source_url: str, page_number: int) -> list[dict[str, object]]:
    board = workday_board(source_url)
    if board is None or page_number < 0:
        raise RuntimeError("invalid or unsupported scoped Workday board")
    offset = page_number * PAGE_SIZE
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.post(
            f"{board.api}/jobs",
            json={"appliedFacets": board.facets, "limit": PAGE_SIZE, "offset": offset, "searchText": ""},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Workday: invalid response")
        jobs, total = payload.get("jobPostings"), payload.get("total")
        if not isinstance(jobs, list) or type(total) is not int or total < 0 or len(jobs) > PAGE_SIZE:
            raise RuntimeError("Workday: invalid job list/total")
        # Workday may reset an out-of-range offset to its first page. Use the
        # current scoped total, not repeated jobs, as the terminal condition.
        if offset > 0 and offset >= total:
            return []
        if (not jobs and offset < total) or (jobs and offset >= total):
            raise RuntimeError("Workday: inconsistent pagination, not an empty board")
        items: list[dict[str, object]] = []
        seen: set[str] = set()
        for job in jobs:
            if not isinstance(job, dict):
                raise RuntimeError("Workday: invalid job row")
            path = job.get("externalPath")
            if (
                not isinstance(path, str)
                or not re.fullmatch(r"/job/[A-Za-z0-9_/%.-]+", path)
                or ".." in path
                or "%" in path
                or path in seen
            ):
                raise RuntimeError("Workday: invalid or duplicate detail path")
            seen.add(path)
            detail = await client.get(f"{board.api}{path}")
            detail.raise_for_status()
            raw = detail.json()
            info = raw.get("jobPostingInfo") if isinstance(raw, dict) else None
            if not isinstance(info, dict) or not isinstance(info.get("title"), str) or not info["title"].strip():
                raise RuntimeError("Workday: invalid job detail")
            description = info.get("jobDescription")
            if not isinstance(description, str) or not description.strip():
                raise RuntimeError("Workday: missing job description")
            text = BeautifulSoup(description, "html.parser").get_text("\n", strip=True)
            compensation, duration, _ = extract_terms(text)
            items.append(
                {
                    "title": info["title"],
                    "url": f"{board.public}{path}",
                    "description": text,
                    "deadline": info.get("endDate") or "",
                    "published": info.get("startDate") or "",
                    "compensation": compensation or "",
                    "duration": duration or "",
                    "language": board.locale.split("-")[0],
                }
            )
        return items
