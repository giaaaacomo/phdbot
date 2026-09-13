"""Incremental ROR catalog import from a local, versioned JSON/ZIP snapshot.

Preview by default. New identities are catalogued but NOT sent to discovery
unless explicitly admitted via --activate-limit. No network or model calls.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import undefer

from phd_searcher.config.database import DatabaseConfig
from phd_searcher.database.models.university import University
from phd_searcher.pipeline.registry_probe import _Record, ror_id
from phd_searcher.pipeline.universities import _COUNTRIES_QUERY, _EXCLUDED_INSTITUTION_IDS

# Keep the established explicit geographical scope; no Wikidata "Europe" inference.
EUROPE = frozenset(re.findall(r'"([A-Z]{2})"', _COUNTRIES_QUERY))
_TYPES = {"education", "facility", "nonprofit"}


class RegistryRecord(_Record):
    external_ids: list[dict[str, object]] = Field(default_factory=list)


def objects(stream: TextIO) -> Iterator[dict[str, object]]:
    """Read a large JSON array with bounded per-record memory, including EOF validation."""
    decoder = json.JSONDecoder()
    buffer = ""
    eof = False

    def fill() -> None:
        nonlocal buffer, eof
        chunk = stream.read(65536)
        eof = not chunk
        buffer += chunk
        if len(buffer) > 4 * 1024 * 1024:
            raise ValueError("registry record exceeds 4 MiB")

    def nonempty() -> None:
        nonlocal buffer
        buffer = buffer.lstrip()
        while not buffer and not eof:
            fill()
            buffer = buffer.lstrip()

    nonempty()
    if not buffer.startswith("["):
        raise ValueError("registry must be a JSON array")
    buffer = buffer[1:]
    first = True
    while True:
        nonempty()
        if buffer.startswith("]"):
            buffer = buffer[1:]
            nonempty()
            if buffer:
                raise ValueError("trailing registry content")
            return
        if not first:
            if not buffer.startswith(","):
                raise ValueError("missing registry comma or closing bracket")
            buffer = buffer[1:]
            nonempty()
        while True:
            try:
                value, end = decoder.raw_decode(buffer)
                break
            except json.JSONDecodeError as exc:
                if eof:
                    raise ValueError("incomplete or malformed registry") from exc
                fill()
        if not isinstance(value, dict):
            raise ValueError("registry record must be an object")
        yield value
        buffer = buffer[end:]
        first = False


def read_snapshot(path: Path) -> Iterator[RegistryRecord]:
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [f for f in archive.infolist() if f.filename.endswith(".json")]
            if len(members) != 1 or members[0].file_size > 512 * 1024 * 1024:
                raise ValueError("expected one ROR JSON snapshot of at most 512 MiB")
            with archive.open(members[0]) as raw, io.TextIOWrapper(raw, encoding="utf-8") as stream:
                for row in objects(stream):
                    yield RegistryRecord.model_validate(row)
    else:
        if path.stat().st_size > 512 * 1024 * 1024:
            raise ValueError("registry snapshot exceeds 512 MiB")
        with path.open(encoding="utf-8") as stream:
            for row in objects(stream):
                yield RegistryRecord.model_validate(row)


def website_key(value: str) -> str:
    """Exact host+path, not shared base domain: labs on a university domain differ."""
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            return ""
        if url.port not in {None, 80, 443}:
            return ""
        return url.hostname.lower().removeprefix("www.") + url.path.rstrip("/")
    except ValueError:
        return ""


def candidate(record: RegistryRecord, countries: set[str], parents: set[str]) -> dict[str, object] | None:
    ror_id(record.id)
    locations = sorted({str(loc.geonames_details.get("country_code", "")) for loc in record.locations})
    home = sorted(set(locations) & countries)
    affiliated = any(r.id in parents and r.type in {"parent", "related"} for r in record.relationships)
    if record.status != "active" or not (_TYPES & set(record.types)) or (not home and not affiliated):
        return None
    qids: set[str] = set()
    for external in record.external_ids:
        if external.get("type") == "wikidata":
            values = external.get("all", [])
            if isinstance(values, list):
                qids.update(str(v) for v in values if re.fullmatch(r"Q[1-9][0-9]*", str(v)))
    if qids & _EXCLUDED_INSTITUTION_IDS:
        return None
    websites = sorted(
        {
            link.value
            for link in record.links
            if link.type == "website" and website_key(link.value) and len(link.value) <= 2048
        }
    )
    name = next((n.value for n in record.names if "ror_display" in n.types), "")
    if (
        not websites
        or not name
        or len(name) > 512
        or not locations
        or not all(re.fullmatch("[A-Z]{2}", c) for c in locations)
    ):
        return None
    return {
        "ror_id": record.id,
        "wikidata_ids": sorted(qids),
        "name": name,
        "country": (home or locations)[0],
        "websites": websites,
        "catalog_tier": "core" if "education" in record.types else "research",
        "assertions": record.model_dump(),
        "scope": "headquarters" if home else "explicit_parent_relationship",
    }


async def import_snapshot(
    session: AsyncSession,
    path: Path,
    *,
    countries: set[str],
    parents: set[str],
    apply: bool = False,
    max_new: int = 100,
    activate_limit: int = 0,
    activate_name: str | None = None,
) -> dict[str, object]:
    """Serialize catalog writers, match only exact IDs; preserve operational state."""
    if max_new < 0 or activate_limit < 0:
        raise ValueError("invalid catalog budgets")
    if not countries or not countries <= EUROPE:
        raise ValueError("countries must be within the configured European scope")
    parents = {ror_id(p) for p in parents}
    # Validation completes before any write: a corrupt final page cannot leave a partial import.
    with path.open("rb") as raw:
        digest = hashlib.file_digest(raw, "sha256").hexdigest()
    rows = []
    seen: set[str] = set()
    scanned = 0
    for record in read_snapshot(path):
        scanned += 1
        if record.id in seen:
            raise ValueError("duplicate ROR ID in snapshot")
        seen.add(record.id)
        item = candidate(record, countries, parents)
        if item is not None:
            rows.append(item)
    rows.sort(key=lambda c: str(c["ror_id"]))
    # Strong lock is brief and only taken after parsing; protects against a
    # concurrent Wikidata refresh creating the same QID during reconciliation.
    if apply:
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(text("LOCK TABLE universities IN SHARE ROW EXCLUSIVE MODE"))
    existing = list((await session.scalars(select(University).options(undefer(University.registry_metadata)))).all())
    by_ror = {u.ror_id: u for u in existing if u.ror_id}
    by_qid = {u.wikidata_id: u for u in existing if u.wikidata_id}
    by_site: dict[str, set[int]] = defaultdict(set)
    by_name: dict[str, set[int]] = defaultdict(set)
    for existing_uni in existing:
        by_site[website_key(existing_uni.website_url)].add(existing_uni.id)
        by_name[existing_uni.name.casefold()].add(existing_uni.id)
    counts: Counter[str] = Counter()
    examples: list[dict[str, object]] = []
    selected = 0
    for item in rows:
        identifier = str(item["ror_id"])
        qids = list(item["wikidata_ids"])  # type: ignore[call-overload]
        websites = list(item["websites"])  # type: ignore[call-overload]
        matches = {u.id: u for q in qids if (u := by_qid.get(q)) is not None}
        if identifier in by_ror:
            u = by_ror[identifier]
            matches[u.id] = u
        uni = next(iter(matches.values()), None)
        conflict = len(matches) > 1 or len(qids) > 1 or (uni is not None and uni.ror_id not in {None, identifier})
        if uni is not None and uni.wikidata_id and qids and uni.wikidata_id not in qids:
            conflict = True
        if uni is None and (any(by_site[website_key(w)] for w in websites) or by_name[str(item["name"]).casefold()]):
            conflict = True  # Inspect, don't conflate a university and its lab.
        if conflict:
            counts["conflicts"] += 1
            if len(examples) < 10:
                examples.append({"ror_id": identifier, "name": item["name"], "action": "identity_conflict"})
            continue
        metadata = {
            "source": "ROR",
            "snapshot": path.name,
            "sha256": digest,
            "scope": item["scope"],
            "record": item["assertions"],
        }
        activate = counts["activated"] < activate_limit and (
            not activate_name or activate_name.casefold() in str(item["name"]).casefold()
        )
        if uni is not None:
            if uni.ror_id == identifier and uni.registry_metadata == metadata:
                counts["unchanged"] += 1
            else:
                counts["linked_or_updated"] += 1
                uni.ror_id = identifier
                uni.registry_metadata = metadata
                # Backfill only: never overwrite audited names/sites or requeue good sources.
                if uni.wikidata_id is None and qids:
                    uni.wikidata_id = qids[0]
                    by_qid[qids[0]] = uni
            by_ror[identifier] = uni
            if activate and uni.discovery_status == "catalogued":
                uni.discovery_status = "pending"
                counts["activated"] += 1
            continue
        counts["new_candidates"] += 1
        if selected >= max_new:
            continue
        selected += 1
        active = activate
        counts["activated" if active else "catalogued"] += 1
        uni = University(
            wikidata_id=qids[0] if qids else None,
            ror_id=identifier,
            name=str(item["name"]),
            country=str(item["country"]),
            website_url=websites[0],
            catalog_tier=str(item["catalog_tier"]),
            catalog_basis="ror:active-research-registry",
            registry_metadata=metadata,
            sitelinks=0,
            discovery_status="pending" if active else "catalogued",
        )
        if apply:
            session.add(uni)
            await session.flush()
        else:
            uni.id = -selected
        by_ror[identifier] = uni
        for q in qids:
            by_qid[q] = uni
        by_site[website_key(uni.website_url)].add(uni.id)
        by_name[uni.name.casefold()].add(uni.id)
    if apply:
        await session.commit()
    else:
        await session.rollback()
    return {
        "applied": apply,
        "snapshot": path.name,
        "sha256": digest,
        "scanned": scanned,
        "in_scope": len(rows),
        "counts": dict(counts),
        "conflict_examples": examples,
        "note": "Catalog identities, not verified vacancies. Existing discovery/positions unchanged.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--countries", default=",".join(sorted(EUROPE)))
    parser.add_argument(
        "--related-to", action="append", default=[], help="Explicit ROR parent for one-hop cross-border scope"
    )
    parser.add_argument("--max-new", type=int, default=100)
    parser.add_argument("--activate-limit", type=int, default=0)
    parser.add_argument("--activate-name", help="Optional name substring for a scoped discovery canary")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    import os

    config = DatabaseConfig(url=os.environ["PHD_SEARCHER__DATABASE__URL"])

    async def run() -> None:
        engine = create_async_engine(config.url)
        try:
            async with AsyncSession(engine) as session:
                report = await import_snapshot(
                    session,
                    args.snapshot,
                    countries=set(args.countries.upper().split(",")),
                    parents=set(args.related_to),
                    apply=args.apply,
                    max_new=args.max_new,
                    activate_limit=args.activate_limit,
                    activate_name=args.activate_name,
                )
                print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            await engine.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
