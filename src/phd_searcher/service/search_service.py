"""Ricerca semantica: embed della query (litellm) + query Qdrant con filtri."""

from __future__ import annotations

from datetime import datetime, time

from injector import inject
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import DatetimeRange, FieldCondition, Filter, MatchValue

from phd_searcher.config.qdrant import QdrantConfig
from phd_searcher.engine.model_helper import ModelHelper
from phd_searcher.typedef.search import SearchBody, SearchHit, SearchResult


class SearchService:
    @inject
    def __init__(self, model: ModelHelper, qdrant: AsyncQdrantClient, config: QdrantConfig) -> None:
        self._model = model
        self._qdrant = qdrant
        self._collection = config.collection

    async def search(self, body: SearchBody) -> SearchResult:
        vector = (await self._model.embed([body.query]))[0]
        must: list[FieldCondition] = []
        if body.country:
            must.append(FieldCondition(key="country", match=MatchValue(value=body.country)))
        if body.university:
            must.append(FieldCondition(key="university", match=MatchValue(value=body.university)))
        if body.deadline_after:
            gte = datetime.combine(body.deadline_after, time.min)
            must.append(FieldCondition(key="deadline", range=DatetimeRange(gte=gte)))
        response = await self._qdrant.query_points(
            self._collection,
            query=vector,
            limit=body.limit,
            query_filter=Filter(must=list(must)) if must else None,
        )
        hits = [
            SearchHit(
                position_id=int(point.id),
                score=point.score,
                title=str((point.payload or {}).get("title", "")),
                university=str((point.payload or {}).get("university", "")),
                country=str((point.payload or {}).get("country", "")),
                url=str((point.payload or {}).get("url", "")),
                deadline=(point.payload or {}).get("deadline"),
            )
            for point in response.points
        ]
        return SearchResult(hits=hits)
