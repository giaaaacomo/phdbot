"""Stadio 1: scarica le università EU da Wikidata e fa upsert in Postgres."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from injector import Injector
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.database.models.university import University
from phd_searcher.pipeline.progress import Progress

_ENDPOINT = "https://query.wikidata.org/sparql"
_USER_AGENT = "phd-searcher/0.1 (research project)"

# Paesi membri EU (P463 Q458) con codice ISO; il FILTER esclude membership terminate (es. GB).
_COUNTRIES_QUERY = """
SELECT ?country ?countryCode WHERE {
  ?country p:P463 ?m .
  ?m ps:P463 wd:Q458 .
  FILTER NOT EXISTS { ?m pq:P582 ?end }
  ?country wdt:P297 ?countryCode .
}
"""

# Università (istanze di Q3918 o sottoclassi) di UN paese, con sito ufficiale e sitelinks.
# Una query per paese: quella unica su tutta l'EU manda in 504 il WDQS.
_UNIS_QUERY = """
SELECT DISTINCT ?u ?uLabel ?website ?sitelinks WHERE {{
  ?u wdt:P31/wdt:P279* wd:Q3918 .
  ?u wdt:P17 wd:{qid} .
  ?u wdt:P856 ?website .
  ?u wikibase:sitelinks ?sitelinks .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


async def _sparql(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    resp = await client.get(
        _ENDPOINT,
        params={"query": query, "format": "json"},
        headers={"User-Agent": _USER_AGENT},
    )
    resp.raise_for_status()
    rows: list[dict[str, Any]] = resp.json()["results"]["bindings"]
    return rows


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    # name_like ignorato: il download da Wikidata è sempre completo
    progress = progress or Progress()
    session_maker = container.get(async_sessionmaker[AsyncSession])
    seen: set[str] = set()
    processed = 0
    async with httpx.AsyncClient(timeout=300) as client:
        countries = await _sparql(client, _COUNTRIES_QUERY)
        pairs = [(c["country"]["value"].rsplit("/", 1)[-1], c["countryCode"]["value"][:2]) for c in countries]
        # ponytail: il membro EU è il Regno dei Paesi Bassi (Q29999, senza codice ISO);
        # le università hanno P17=Q55 (Paesi Bassi), che la query non vede
        if "NL" not in {code for _, code in pairs}:
            pairs.append(("Q55", "NL"))
        await progress.begin(len(pairs))
        async with session_maker() as session:
            for qid, code in pairs:
                if progress.should_stop:
                    break
                await progress.tick(code)
                try:
                    rows = await _sparql(client, _UNIS_QUERY.format(qid=qid))
                except httpx.HTTPError:
                    try:  # un retry: il WDQS dà 504 sporadici sui paesi grossi
                        await asyncio.sleep(5)
                        rows = await _sparql(client, _UNIS_QUERY.format(qid=qid))
                    except httpx.HTTPError as exc:  # un paese fallito non ferma la run (rilanciabile)
                        print(f"universities: query failed for {code}: {exc}")
                        continue
                for row in rows:
                    wikidata_id = row["u"]["value"].rsplit("/", 1)[-1]
                    if wikidata_id in seen:  # SPARQL può duplicare per sito multiplo
                        continue
                    seen.add(wikidata_id)
                    sitelinks = int(row["sitelinks"]["value"])
                    stmt = (
                        pg_insert(University)
                        .values(
                            wikidata_id=wikidata_id,
                            name=row["uLabel"]["value"][:512],
                            country=code,
                            website_url=row["website"]["value"][:2048],
                            sitelinks=sitelinks,
                        )
                        .on_conflict_do_update(index_elements=["wikidata_id"], set_={"sitelinks": sitelinks})
                    )
                    await session.execute(stmt)
                    processed += 1
                    if limit is not None and processed >= limit:
                        break
                await session.commit()
                print(f"universities: {code}: {len(rows)} rows")
                if limit is not None and processed >= limit:
                    break
                await asyncio.sleep(1)  # gentilezza verso il WDQS
    print(f"universities: {processed} processed")
    return processed
