from datetime import date

from qdrant_client.models import Distance, PointStruct, VectorParams

from phd_searcher.service.search_service import SearchService
from phd_searcher.typedef.search import SearchBody

PAYLOAD = {
    "title": "PhD in Robotics",
    "url": "https://uni.example/jobs/1",
    "university": "Uni Example",
    "country": "IT",
    "deadline": "2099-01-01",
}


async def _seed(qdrant):
    await qdrant.create_collection("positions", vectors_config=VectorParams(size=4, distance=Distance.COSINE))
    await qdrant.upsert("positions", points=[PointStruct(id=1, vector=[1.0, 0.0, 0.0, 0.0], payload=PAYLOAD)])


async def test_search_returns_seeded_point(container, qdrant):
    await _seed(qdrant)
    service = container.get(SearchService)
    result = await service.search(SearchBody(query="robotics"))
    assert result.hits[0].position_id == 1
    assert result.hits[0].country == "IT"


async def test_search_country_filter_excludes(container, qdrant):
    await _seed(qdrant)
    service = container.get(SearchService)
    result = await service.search(SearchBody(query="robotics", country="DE"))
    assert result.hits == []


async def test_search_deadline_filter(container, qdrant):
    await _seed(qdrant)
    service = container.get(SearchService)
    kept = await service.search(SearchBody(query="robotics", deadline_after=date(2050, 1, 1)))
    assert [h.position_id for h in kept.hits] == [1]
    dropped = await service.search(SearchBody(query="robotics", deadline_after=date(2100, 1, 1)))
    assert dropped.hits == []
