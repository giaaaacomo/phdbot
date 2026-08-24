"""Stadio 1: scarica gli istituti europei di alta formazione e fa upsert in Postgres."""

from __future__ import annotations

import asyncio
from typing import TypedDict, cast

import httpx
from injector import Injector
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.database.models.university import University
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.retry import RetryExhaustedError, RetryInterruptedError, retry_async

_ENDPOINT = "https://query.wikidata.org/sparql"
_USER_AGENT = "phd-searcher/0.1 (research project)"


class _SparqlBinding(TypedDict):
    value: str


type _SparqlRow = dict[str, _SparqlBinding]
type _SparqlPayload = dict[str, dict[str, list[_SparqlRow]]]

# Perimetro europeo esplicito: EU/EEA, Regno Unito, Svizzera, microstati,
# Balcani, Moldova, Ucraina e Bielorussia. Esclude deliberatamente gli stati
# transcontinentali/caucasici che Wikidata associa anche a Q46 (RU, TR, GE, AM, AZ, KZ).
_COUNTRIES_QUERY = """
SELECT DISTINCT ?country ?countryCode WHERE {
  VALUES ?countryCode {
    "AL" "AD" "AT" "BY" "BE" "BA" "BG" "HR" "CY" "CZ" "DK" "EE"
    "FI" "FR" "DE" "GR" "HU" "IS" "IE" "IT" "XK" "LV" "LI" "LT"
    "LU" "MT" "MD" "MC" "ME" "NL" "MK" "NO" "PL" "PT" "RO" "SM"
    "RS" "SK" "SI" "ES" "SE" "CH" "UA" "GB" "VA"
  }
  ?country wdt:P297 ?countryCode.
  ?country wdt:P31/wdt:P279* wd:Q3624078.
}
"""

# Livello 1: università (Q3918 e sottoclassi) di UN paese, con sito ufficiale.
_UNIS_QUERY = """
SELECT DISTINCT ?u ?uLabel ?uDescription ?website ?sitelinks WHERE {{
  ?u wdt:P31/wdt:P279* wd:Q3918 .
  FILTER(?u NOT IN (
    wd:Q1322289, wd:Q3955061, wd:Q65132252, wd:Q4470499,
    wd:Q3577898, wd:Q273493
  ))
  ?u wdt:P17 wd:{qid} .
  ?u wdt:P856 ?website .
  ?u wikibase:sitelinks ?sitelinks .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,it". }}
}}
ORDER BY DESC(?sitelinks) ?u
"""

# Falsi positivi confermati: consorzio dissolto, seminari/edifici e un editore.
# Il filtro nella query impedisce che rientrino ai refresh successivi.
_EXCLUDED_INSTITUTION_IDS = {
    "Q1322289",  # dissolved Lille consortium
    "Q3955061",  # diocesan seminary
    "Q65132252",  # seminary building
    "Q4470499",  # academic publisher, not the Poltava university
    "Q3577898",  # Chetham's: specialist school for ages 8-18
    "Q273493",  # Supelec merged into CentraleSupelec in 2015
}

# Wikidata P856 può restare obsoleto o venire riutilizzato da terzi. Questi URL
# sono stati verificati sui siti ufficiali durante l'audit del catalogo.
_WEBSITE_OVERRIDES = {
    "Q11815245": "https://uczelniaoswiecim.edu.pl/en/",
    "Q1782547": "https://cons.bz.it/it/",
}

# Livello 2: istituzioni universitarie specialistiche che Wikidata non mette sotto
# Q3918. Le classi sono intenzionalmente esplicite; ROR/WHED e almeno due sitelink
# sono un gate conservativo contro l'esplosione del catalogo generico.
_SPECIALIST_CLASSES = {
    "Q17028020": "vocational university",
    "Q1365560": "university of applied sciences (Germany)",
    "Q15850083": "university of applied sciences (Switzerland)",
    "Q1941786": "university of applied sciences (Netherlands)",
    "Q383092": "art academy",
    "Q380093": "film school",
    "Q21540699": "design school",
    "Q184644": "conservatory",
    "Q847027": "grande ecole",
    "Q1371037": "institute of technology",
    "Q135436": "ecole normale superieure",
}
_SPECIALISTS_QUERY = """
SELECT DISTINCT ?u ?uLabel ?uDescription ?website ?sitelinks ?specialistClass WHERE {{
  VALUES ?specialistClass {{ {specialist_classes} }}
  ?u wdt:P31 ?specialistClass .
  ?u wdt:P17 wd:{qid} .
  ?u wdt:P856 ?website .
  ?u wikibase:sitelinks ?sitelinks .
  FILTER(?sitelinks >= 2)
  FILTER(EXISTS {{ ?u wdt:P6782 ?ror }} || EXISTS {{ ?u wdt:P5584 ?whed }})
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,it". }}
}}
ORDER BY DESC(?sitelinks) ?u
"""

# Le eccezioni ufficialmente verificate possono superare il gate incompleto di
# Wikidata. Usano comunque il vero QID, evitando duplicati futuri.
_CURATED_INSTITUTIONS = (
    {
        "wikidata_id": "Q3577724",
        "name": "ECAL/University of Art and Design Lausanne",
        "country": "CH",
        "website_url": "https://ecal.ch/en/",
        "description": "University of art and design in Lausanne, Switzerland",
        "sitelinks": 5,
        "catalog_tier": "specialist",
        "catalog_basis": "curated:official-site;wikidata:Q3577724",
    },
    {
        "wikidata_id": "Q3128581",
        "name": "HEAD - Geneva University of Art and Design",
        "country": "CH",
        "website_url": "https://www.hesge.ch/head/en",
        "description": "University-level art and design school with Bachelor, Master, doctoral and research activity",
        "sitelinks": 4,
        "catalog_tier": "specialist",
        "catalog_basis": "curated:official-site;wikidata:Q3128581",
    },
    {
        "wikidata_id": "Q2504327",
        "name": "International University of Monaco",
        "country": "MC",
        "website_url": "https://www.monaco.edu/en/",
        "description": "Government-recognized private higher education institution offering Bachelor, Master, MBA and DBA programs",
        "sitelinks": 3,
        "catalog_tier": "specialist",
        "catalog_basis": "curated:government-recognition;wikidata:Q2504327",
    },
)


def _checkpoint_int(value: object) -> int:
    return int(value) if isinstance(value, (int, float, str)) else 0


def _should_stop(progress: Progress) -> bool:
    return progress.should_stop


def _optional_binding_value(row: _SparqlRow, key: str) -> str | None:
    binding = row.get(key)
    return None if binding is None else binding.get("value")


async def _sparql(client: httpx.AsyncClient, query: str) -> list[_SparqlRow]:
    resp = await client.get(
        _ENDPOINT,
        params={"query": query, "format": "json"},
        headers={"User-Agent": _USER_AGENT},
    )
    resp.raise_for_status()
    payload = cast(_SparqlPayload, resp.json())
    return payload["results"]["bindings"]


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
    checkpoint = await progress.load_checkpoint()
    raw_completed = checkpoint.get("completed_countries", [])
    completed = {str(value) for value in raw_completed} if isinstance(raw_completed, list) else set()
    processed = _checkpoint_int(checkpoint.get("processed", 0))
    active_country = str(checkpoint.get("active_country") or "")
    country_offset = _checkpoint_int(checkpoint.get("country_offset", 0))
    async with httpx.AsyncClient(timeout=300) as client:
        async def fetch_countries() -> list[_SparqlRow]:
            return await _sparql(client, _COUNTRIES_QUERY)

        try:
            countries = await retry_async(
                progress,
                "wikidata:countries",
                fetch_countries,
                base_delay=30,
                max_delay=900,
            )
        except RetryInterruptedError:
            return processed
        pairs = [(c["country"]["value"].rsplit("/", 1)[-1], c["countryCode"]["value"][:2]) for c in countries]
        # ponytail: il membro EU è il Regno dei Paesi Bassi (Q29999, senza codice ISO);
        # le università hanno P17=Q55 (Paesi Bassi), che la query non vede
        if "NL" not in {code for _, code in pairs}:
            pairs.append(("Q55", "NL"))
        pairs = [(qid, code) for qid, code in pairs if code not in completed]
        await progress.begin(len(pairs))
        async with session_maker() as session:
            for institution in _CURATED_INSTITUTIONS:
                stmt = (
                    pg_insert(University)
                    .values(**institution)
                    .on_conflict_do_update(
                        index_elements=["wikidata_id"],
                        set_={key: value for key, value in institution.items() if key != "wikidata_id"},
                    )
                )
                await session.execute(stmt)
            await session.commit()

            for qid, code in pairs:
                if _should_stop(progress):
                    break
                await progress.tick(code)
                if progress.should_stop:
                    break
                try:
                    async def fetch_core(current_qid: str = qid) -> list[_SparqlRow]:
                        return await _sparql(client, _UNIS_QUERY.format(qid=current_qid))

                    core_rows = await retry_async(
                        progress,
                        f"wikidata:{code}:core",
                        fetch_core,
                        base_delay=30,
                        max_delay=900,
                    )
                    # WDQS applica rate limiting per client: separare le due query
                    # evita che quella specialistica parta nella stessa raffica.
                    await asyncio.sleep(5)

                    async def fetch_specialists(current_qid: str = qid) -> list[_SparqlRow]:
                        return await _sparql(
                            client,
                            _SPECIALISTS_QUERY.format(
                                qid=current_qid,
                                specialist_classes=" ".join(
                                    f"wd:{item}" for item in _SPECIALIST_CLASSES
                                ),
                            ),
                        )

                    specialist_rows = await retry_async(
                        progress,
                        f"wikidata:{code}:specialist",
                        fetch_specialists,
                        base_delay=30,
                        max_delay=900,
                    )
                except RetryInterruptedError:
                    break
                except RetryExhaustedError as exc:
                    # Non dichiarare mai completato un paese che Wikidata non ha restituito:
                    # la run fallisce e Resume riparte dal checkpoint dello stesso paese.
                    raise RuntimeError(
                        f"Wikidata unavailable for {code} after retries; resume to retry it"
                    ) from exc

                rows: list[tuple[_SparqlRow, str, str]] = [
                    (row, "core", "wikidata:Q3918") for row in core_rows
                ]
                rows.extend(
                    (
                        row,
                        "specialist",
                        f"wikidata:{row['specialistClass']['value'].rsplit('/', 1)[-1]}",
                    )
                    for row in specialist_rows
                )

                start = country_offset if active_country == code else 0
                seen = {
                    row["u"]["value"].rsplit("/", 1)[-1]
                    for row, _catalog_tier, _catalog_basis in rows[:start]
                }
                for offset, (row, catalog_tier, catalog_basis) in enumerate(
                    rows[start:],
                    start=start,
                ):
                    await progress.check_stop()
                    if _should_stop(progress):
                        break
                    wikidata_id = row["u"]["value"].rsplit("/", 1)[-1]
                    if wikidata_id in seen:  # siti ufficiali multipli possono duplicare lo stesso ateneo
                        active_country = code
                        country_offset = offset + 1
                        await progress.save_checkpoint(
                            active_country=active_country,
                            country_offset=country_offset,
                        )
                        continue
                    seen.add(wikidata_id)
                    sitelinks = int(row["sitelinks"]["value"])
                    website_url = _WEBSITE_OVERRIDES.get(wikidata_id, row["website"]["value"])
                    stmt = (
                        pg_insert(University)
                        .values(
                            wikidata_id=wikidata_id,
                            name=row["uLabel"]["value"][:512],
                            country=code,
                            website_url=website_url[:2048],
                            description=_optional_binding_value(row, "uDescription"),
                            sitelinks=sitelinks,
                            catalog_tier=catalog_tier,
                            catalog_basis=catalog_basis,
                        )
                        .on_conflict_do_update(
                            index_elements=["wikidata_id"],
                            set_={
                                "name": row["uLabel"]["value"][:512],
                                "country": code,
                                "website_url": website_url[:2048],
                                "description": _optional_binding_value(row, "uDescription"),
                                "sitelinks": sitelinks,
                                "catalog_tier": catalog_tier,
                                "catalog_basis": catalog_basis,
                            },
                        )
                    )
                    await session.execute(stmt)
                    await session.commit()
                    processed += 1
                    active_country = code
                    country_offset = offset + 1
                    await progress.save_checkpoint(
                        processed=processed,
                        active_country=active_country,
                        country_offset=country_offset,
                    )
                    if limit is not None and processed >= limit:
                        break
                if _should_stop(progress):
                    break
                print(
                    f"universities: {code}: {len(core_rows)} core + "
                    f"{len(specialist_rows)} specialist rows"
                )
                if limit is not None and processed >= limit:
                    break
                completed.add(code)
                active_country = ""
                country_offset = 0
                await progress.save_checkpoint(
                    completed_countries=sorted(completed),
                    active_country=active_country,
                    country_offset=country_offset,
                )
                await asyncio.sleep(1)  # gentilezza verso il WDQS
    print(f"universities: {processed} processed")
    return processed
