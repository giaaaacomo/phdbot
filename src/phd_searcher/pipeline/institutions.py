"""Indice semantico separato per università, istituzioni e gruppi di ricerca."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import cast

from injector import Injector
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointIdsList, PointStruct, VectorParams
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.clock import local_today
from phd_searcher.config.qdrant import QdrantConfig
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.engine.model_helper import ModelHelper
from phd_searcher.engine.search_contract import validate_search_index_contract
from phd_searcher.engine.search_documents import (
    SEARCH_INDEX_CONTRACT_PAYLOAD,
    build_institution_search_document,
)
from phd_searcher.opportunity_kinds import PROGRAMME, SPONTANEOUS, VACANCY
from phd_searcher.pipeline.progress import Progress

_BATCH = 64
_ACTIVE_OPPORTUNITY_KINDS = frozenset((VACANCY, PROGRAMME))
_INSTITUTION_SOURCE_KINDS = (VACANCY, PROGRAMME, SPONTANEOUS)
_MAX_ENTITY_SNIPPETS = 20


def _position_text(position: Position) -> str:
    return f"{position.title} {position.area or ''}\n{position.full_description or position.description}"


def _build_entities(
    universities: list[University],
    position_rows: list[tuple[Position, University | None]],
    *,
    name_like: str | None = None,
    today: date | None = None,
) -> list[dict[str, object]]:
    """Build institution payloads without treating spontaneous links as vacancies."""
    needle = name_like.casefold() if name_like else None
    current_day = today or local_today()
    position_rows = [
        (position, university)
        for position, university in position_rows
        if position.is_active
        and position.screening_status == "eligible"
        and position.opportunity_kind in _INSTITUTION_SOURCE_KINDS
        and (position.deadline is None or position.deadline >= current_day)
    ]
    titles: dict[int, list[str]] = defaultdict(list)
    spontaneous_snippets: dict[int, list[str]] = defaultdict(list)
    counts: dict[int, int] = defaultdict(int)
    discovered_spontaneous_urls: dict[int, str] = {}

    for position, university in position_rows:
        if university is None:
            continue
        if position.opportunity_kind in _ACTIVE_OPPORTUNITY_KINDS:
            counts[university.id] += 1
            if len(titles[university.id]) < _MAX_ENTITY_SNIPPETS:
                titles[university.id].append(f"{position.title} {position.area or ''}")
        elif position.opportunity_kind == SPONTANEOUS:
            discovered_spontaneous_urls.setdefault(university.id, position.url)
            if len(spontaneous_snippets[university.id]) < _MAX_ENTITY_SNIPPETS:
                spontaneous_snippets[university.id].append(_position_text(position))

    entities: list[dict[str, object]] = []
    for university in universities:
        if needle and needle not in university.name.casefold():
            continue
        entities.append(
            {
                "id": university.id,
                "name": university.name,
                "kind": "university",
                "university": university.name,
                "country": university.country,
                "url": university.website_url,
                # Il link curato sull'anagrafica resta autorevole; una pagina
                # classificata spontaneous è un fallback, non lo sovrascrive.
                "spontaneous_application_url": (
                    university.spontaneous_application_url
                    or discovered_spontaneous_urls.get(university.id)
                ),
                "active_positions": counts[university.id],
                "text": "\n".join(
                    [
                        university.name,
                        university.description or "",
                        *titles[university.id],
                        *spontaneous_snippets[university.id],
                    ]
                ),
            }
        )

    groups: dict[tuple[str, str], dict[str, object]] = {}
    standalone: dict[tuple[str, str], dict[str, object]] = {}
    for position, university in position_rows:
        country = university.country if university else (position.institution_country or "")
        institution = university.name if university else (position.institution_name or "")
        if needle and needle not in institution.casefold():
            continue

        if position.research_group:
            key = (position.research_group.casefold(), country)
            group = groups.setdefault(
                key,
                {
                    "id": 10_000_000 + position.id,
                    "name": position.research_group,
                    "kind": "research_group",
                    "university": institution,
                    "country": country,
                    "url": position.url,
                    "spontaneous_application_url": None,
                    "active_positions": 0,
                    "snippets": [],
                },
            )
            snippets = cast(list[str], group["snippets"])
            if len(snippets) < _MAX_ENTITY_SNIPPETS:
                snippets.append(_position_text(position))
            if position.opportunity_kind in _ACTIVE_OPPORTUNITY_KINDS:
                group["active_positions"] = cast(int, group["active_positions"]) + 1
                # La destinazione principale del gruppo resta un bando attivo
                # quando disponibile; il link spontaneo ha un campo dedicato.
                group["url"] = position.url
            elif position.opportunity_kind == SPONTANEOUS:
                group["spontaneous_application_url"] = (
                    group["spontaneous_application_url"] or position.url
                )
            continue

        # Una candidatura spontanea da aggregatore può non avere university_id
        # né research_group. Il payload InstitutionHit supporta già kind/url e
        # non richiede una nuova tabella per renderla ricercabile.
        if position.opportunity_kind == SPONTANEOUS and institution:
            key = (institution.casefold(), country)
            entity = standalone.setdefault(
                key,
                {
                    "id": 20_000_000 + position.id,
                    "name": institution,
                    "kind": "institution",
                    "university": institution,
                    "country": country,
                    "url": position.url,
                    "spontaneous_application_url": position.url,
                    "active_positions": 0,
                    "snippets": [],
                },
            )
            snippets = cast(list[str], entity["snippets"])
            if len(snippets) < _MAX_ENTITY_SNIPPETS:
                snippets.append(_position_text(position))

    for entity in [*groups.values(), *standalone.values()]:
        snippets = cast(list[str], entity.pop("snippets"))
        entity["text"] = "\n".join(
            [
                str(entity["name"]),
                str(entity["university"]),
                *snippets,
            ]
        )
        entities.append(entity)

    return entities


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    progress = progress or Progress()
    session_maker = container.get(async_sessionmaker[AsyncSession])
    model = container.get(ModelHelper)
    qdrant = container.get(AsyncQdrantClient)
    collection = f"{container.get(QdrantConfig).collection}_institutions"
    full_refresh = limit is None and name_like is None

    await validate_search_index_contract(
        qdrant,
        collection,
        model.search_index_contract(institutions=True),
    )

    async with session_maker() as session:
        universities = (await session.execute(select(University).order_by(University.id))).scalars().all()
        position_rows = (
            await session.execute(
                select(Position, University)
                .outerjoin(University, Position.university_id == University.id)
                .where(Position.is_active.is_(True))
                .where(Position.screening_status == "eligible")
                .where(Position.opportunity_kind.in_(_INSTITUTION_SOURCE_KINDS))
                .where(or_(Position.deadline.is_(None), Position.deadline >= local_today()))
                .order_by(Position.id)
            )
        ).all()

    entities = _build_entities(
        list(universities),
        [(row[0], row[1]) for row in position_rows],
        name_like=name_like,
    )

    if limit is not None:
        entities = entities[:limit]
    current_ids: set[int] = set()
    for entity in entities:
        entity_id = entity["id"]
        if isinstance(entity_id, int):
            current_ids.add(entity_id)
    previous_ids: set[int] = set()
    if full_refresh and await qdrant.collection_exists(collection):
        offset = None
        while True:
            points, offset = await qdrant.scroll(
                collection,
                limit=256,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            previous_ids.update(point.id for point in points if isinstance(point.id, int))
            if offset is None:
                break
    await progress.begin((len(entities) + _BATCH - 1) // _BATCH)
    indexed = 0
    for start in range(0, len(entities), _BATCH):
        if progress.should_stop:
            break
        batch = entities[start : start + _BATCH]
        await progress.tick(f"institutions {start + 1}-{start + len(batch)}")
        vectors = await model.embed_documents(
            [
                build_institution_search_document(
                    name=str(entity["name"]),
                    university=str(entity["university"]),
                    kind=str(entity["kind"]),
                    text=str(entity["text"]),
                )
                for entity in batch
            ]
        )
        if not await qdrant.collection_exists(collection):
            await qdrant.create_collection(
                collection,
                vectors_config=VectorParams(size=len(vectors[0]), distance=Distance.COSINE),
            )
        await qdrant.upsert(
            collection,
            points=[
                PointStruct(
                    id=cast(int, entity["id"]),
                    vector=vector,
                    payload={
                        **{
                            key: value
                            for key, value in entity.items()
                            if key not in ("id", "text")
                        },
                        SEARCH_INDEX_CONTRACT_PAYLOAD: model.search_index_contract(
                            institutions=True
                        ),
                    },
                )
                for entity, vector in zip(batch, vectors, strict=True)
            ],
            wait=True,
        )
        indexed += len(batch)
        await progress.save_checkpoint(processed=indexed)
    if full_refresh and not progress.should_stop:
        stale_ids = sorted(previous_ids - current_ids)
        if stale_ids:
            await qdrant.delete(collection, points_selector=PointIdsList(points=stale_ids), wait=True)
            print(f"institutions: removed {len(stale_ids)} stale entities")
    return indexed
