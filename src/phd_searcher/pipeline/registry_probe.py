"""Budgeted, cached ROR relationship discovery. Preview only; never imports jobs.

Run with ``python -m phd_searcher.pipeline.registry_probe --help``. The registry
is queried by an existing organisation's ID, not a hard-coded child/lab name.
Returned websites and relationships are evidence to resolve, not permission to
attribute a partner's vacancies to its parent or to crawl arbitrary sites.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError

_ENDPOINT = "https://api.ror.org/v2/organizations"
_PAGE_SIZE = 20
_CACHE_DAYS = 30


def _cooldown(value: str) -> float:
    """Honor both Retry-After formats, with a one-hour minimum backoff."""
    if value.isdigit():
        return max(3600, int(value))
    try:
        return max(3600, parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return 3600


def _validate_page(payload: _Page, page: int) -> None:
    expected = min(_PAGE_SIZE, max(0, payload.number_of_results - (page - 1) * _PAGE_SIZE))
    if len(payload.items) != expected:
        raise ValueError("registry page incomplete or inconsistent with reported total")
    ids = [ror_id(record.id) for record in payload.items]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate registry IDs within page")


def ror_id(value: str) -> str:
    identifier = value.removeprefix("https://ror.org/")
    if not re.fullmatch(r"0[0-9a-hjkmnp-tv-z]{6}[0-9]{2}", identifier):
        raise ValueError("use a ROR ID or canonical https://ror.org/ URL")
    return f"https://ror.org/{identifier}"


class _Name(BaseModel):
    value: str
    types: list[str]


class _Link(BaseModel):
    type: str
    value: str


class _Relationship(BaseModel):
    id: str
    type: str
    label: str = ""


class _Location(BaseModel):
    geonames_details: dict[str, object]


class _Record(BaseModel):
    id: str
    status: Literal["active", "inactive", "withdrawn"]
    names: list[_Name]
    types: list[str]
    links: list[_Link]
    relationships: list[_Relationship]
    locations: list[_Location]


class _Page(BaseModel):
    number_of_results: int = Field(ge=0)
    items: list[_Record]


@dataclass
class ProbeReport:
    seed: str
    headquarters_country: str | None
    state: str = "partial"
    next_page: int | None = 1
    registry_total: int | None = None
    network_requests: int = 0
    cache_hits: int = 0
    pages_read: int = 0
    elapsed_seconds: float = 0
    reason: str | None = None
    candidates: list[dict[str, object]] = field(default_factory=list)


def _candidate(record: _Record, seed: str, country: str | None) -> dict[str, object] | None:
    identifier = ror_id(record.id)
    relations = [r for r in record.relationships if r.id == seed and r.type in {"parent", "related"}]
    countries = sorted({str(loc.geonames_details.get("country_code", "")) for loc in record.locations} - {""})
    if record.status != "active" or identifier == seed or not relations or (country and country not in countries):
        return None
    websites = []
    for link in record.links:
        try:
            parsed = urlsplit(link.value)
        except ValueError:
            continue
        if (
            link.type == "website"
            and parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        ):
            websites.append(link.value)
    names = [name.value for name in record.names]
    display = next((name.value for name in record.names if "ror_display" in name.types), None)
    if not display or not websites:
        return None
    return {
        "ror_id": identifier,
        "name": display,
        "aliases": sorted(set(names) - {display}),
        "types": record.types,
        "websites": sorted(set(websites)),
        "headquarters_countries": countries,
        "relations_to_seed": [r.model_dump() for r in relations],
        "source": f"{_ENDPOINT}/{identifier.rsplit('/', 1)[-1]}",
        "disposition": "identity_candidate_not_imported",
    }


async def probe_relations(
    seed: str,
    *,
    cache_path: Path,
    country: str | None = None,
    start_page: int = 1,
    max_pages: int = 2,
    max_requests: int = 2,
    seconds: float = 30,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProbeReport:
    """One bounded slice; persist successful pages and error cooldowns locally.

    ``next_page`` is an explicit resumable cursor, not a claim of completeness.
    No recursive relation expansion, model call, main DB write or website fetch.
    """
    seed = ror_id(seed)
    country = country.upper() if country else None
    if country and not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("country must be ISO alpha-2")
    if not (1 <= start_page <= 500 and 1 <= max_pages <= 10 and 0 <= max_requests <= 10 and 0 < seconds <= 120):
        raise ValueError("invalid probe budget")
    started = time.monotonic()
    report = ProbeReport(seed=seed, headquarters_country=country, next_page=start_page)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = sqlite3.connect(cache_path)
    cache.execute(
        "CREATE TABLE IF NOT EXISTS ror_pages (key TEXT PRIMARY KEY, expires REAL NOT NULL, body TEXT, error TEXT)"
    )
    query = "relationships.id:" + seed.replace(":", "\\:").replace("/", "\\/")
    seen: set[str] = set()
    try:
        async with (
            asyncio.timeout(seconds),
            httpx.AsyncClient(timeout=min(seconds, 15), follow_redirects=False, transport=transport) as client,
        ):
            for page in range(start_page, min(start_page + max_pages, 501)):
                params = {"query.advanced": query, "page": str(page)}
                if country:
                    params["filter"] = f"locations.geonames_details.country_code:{country}"
                key = json.dumps(params, sort_keys=True)
                cached = cache.execute(
                    "SELECT body,error FROM ror_pages WHERE key=? AND expires>?", (key, time.time())
                ).fetchone()
                if cached is not None:
                    report.cache_hits += 1
                    if cached[1]:
                        report.state, report.reason = "deferred", cached[1]
                        break
                    payload = _Page.model_validate_json(cached[0])
                else:
                    if report.network_requests >= max_requests:
                        report.reason = "network_budget"
                        break
                    report.network_requests += 1
                    response = await client.get(_ENDPOINT, params=params)
                    if response.status_code != 200:
                        error = f"registry HTTP {response.status_code}; deferred, not an empty result"
                        # No sleeping/retrying inside a pipeline. A future slice
                        # can retry after the durable cooldown, including 429s.
                        retry_after = response.headers.get("Retry-After", "")
                        delay = _cooldown(retry_after)
                        cache.execute(
                            "INSERT OR REPLACE INTO ror_pages VALUES (?,?,?,?)", (key, time.time() + delay, None, error)
                        )
                        cache.commit()
                        report.state, report.reason = "deferred", error
                        break
                    payload = _Page.model_validate(response.json())
                    _validate_page(payload, page)
                    cache.execute(
                        "INSERT OR REPLACE INTO ror_pages VALUES (?,?,?,NULL)",
                        (key, time.time() + _CACHE_DAYS * 86400, payload.model_dump_json()),
                    )
                    cache.commit()
                _validate_page(payload, page)
                if report.registry_total is not None and report.registry_total != payload.number_of_results:
                    raise ValueError("registry total changed between pages; restart preview")
                report.registry_total = payload.number_of_results
                for record in payload.items:
                    candidate = _candidate(record, seed, country)
                    if candidate is not None and record.id not in seen:
                        report.candidates.append(candidate)
                        seen.add(record.id)
                report.pages_read += 1
                report.next_page = page + 1
                if page * _PAGE_SIZE >= payload.number_of_results:
                    report.state, report.next_page = "complete", None
                    break
            if report.state == "partial" and report.reason is None:
                report.reason = "api_10000_limit_use_registry_dump" if report.next_page == 501 else "page_budget"
    except TimeoutError:
        report.reason = "time_budget"
    except (httpx.HTTPError, ValidationError, ValueError) as exc:
        report.state, report.reason = "failed", str(exc)[:400]
    finally:
        cache.close()
    report.elapsed_seconds = round(time.monotonic() - started, 3)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True, help="Known organisation ROR ID (not a child name)")
    parser.add_argument("--country", help="Optional headquarters country; NOT opportunity location")
    parser.add_argument("--cache", type=Path, default=Path("exports/registry-probe.sqlite3"))
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--max-requests", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=30)
    args = parser.parse_args()
    result = asyncio.run(
        probe_relations(
            args.parent,
            cache_path=args.cache,
            country=args.country,
            start_page=args.start_page,
            max_pages=args.max_pages,
            max_requests=args.max_requests,
            seconds=args.seconds,
        )
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
