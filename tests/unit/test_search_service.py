from datetime import date

import pytest
from qdrant_client.models import Distance, PointStruct, VectorParams

from phd_searcher.service.search_service import SearchService, normalize_retrieval_query
from phd_searcher.typedef.search import SearchBody


@pytest.mark.parametrize(
    ("query", "normalized"),
    [
        ("XR", "extended reality"),
        ("VR interaction", "virtual reality interaction"),
        ("AR/VR for HCI", "augmented reality/virtual reality for human-computer interaction"),
        ("realtà estesa e HCI", "extended reality e human-computer interaction"),
        ("realta virtuale", "virtual reality"),
        ("calcolo spaziale", "spatial computing"),
        ("design dell'interazione", "interaction design"),
        ("progettazione dell\u2019interazione", "interaction design"),
        ("ingegneria navale", "naval engineering"),
        ("xray and architecture", "xray and architecture"),
    ],
)
def test_retrieval_query_expands_only_standalone_domain_acronyms(
    query: str,
    normalized: str,
) -> None:
    assert normalize_retrieval_query(query) == normalized


PAYLOAD = {
    "title": "PhD in Robotics",
    "url": "https://uni.example/jobs/1",
    "university": "Uni Example",
    "country": "IT",
    "opportunity_kind": "vacancy",
    "verification_status": "verified",
    "confidence": 0.98,
    "uncertainty_percent": 0,
    "uncertainty_flags": [],
    "deadline": "2099-01-01",
    "deadline_ts": "2099-01-01T00:00:00+00:00",
    "first_seen_at": "2026-08-20T08:00:00+00:00",
    "last_seen_at": "2026-08-24T08:00:00+00:00",
    "scraped_at": "2026-08-24T08:00:00+00:00",
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
    assert result.hits[0].opportunity_kind == "vacancy"
    assert result.hits[0].verification_status == "verified"
    assert result.hits[0].confidence == 0.98
    assert result.hits[0].first_seen_at.isoformat() == "2026-08-20T08:00:00+00:00"
    assert result.hits[0].last_seen_at.isoformat() == "2026-08-24T08:00:00+00:00"


async def test_search_defaults_to_verified_and_can_include_probable(container, qdrant):
    await _seed(qdrant)
    await qdrant.upsert(
        "positions",
        points=[
            PointStruct(
                id=2,
                vector=[1.0, 0.0, 0.0, 0.0],
                payload={
                    **PAYLOAD,
                    "title": "Possible PhD in Robotics",
                    "url": "https://uni.example/jobs/2",
                    "verification_status": "probable",
                    "confidence": 0.72,
                    "uncertainty_percent": 60,
                    "uncertainty_flags": ["open_status", "details"],
                },
            )
        ],
    )
    service = container.get(SearchService)

    verified = await service.search(SearchBody(query="robotics"))
    inclusive = await service.search(
        SearchBody(query="robotics", mode="include_probable")
    )

    assert [hit.position_id for hit in verified.hits] == [1]
    assert {hit.position_id for hit in inclusive.hits} == {1, 2}
    probable = next(hit for hit in inclusive.hits if hit.position_id == 2)
    assert probable.verification_status == "probable"
    assert probable.confidence == 0.72
    assert probable.uncertainty_percent == 60
    assert probable.uncertainty_flags == ["open_status", "details"]


async def test_search_filters_and_orders_by_maximum_uncertainty(container, qdrant):
    await _seed(qdrant)
    await qdrant.upsert(
        "positions",
        points=[
            PointStruct(
                id=2,
                vector=[1.0, 0.0, 0.0, 0.0],
                payload={
                    **PAYLOAD,
                    "title": "Grounded probable",
                    "url": "https://uni.example/jobs/2",
                    "verification_status": "probable",
                    "uncertainty_percent": 35,
                    "uncertainty_flags": ["open_status"],
                },
            ),
            PointStruct(
                id=3,
                vector=[1.0, 0.0, 0.0, 0.0],
                payload={
                    **PAYLOAD,
                    "title": "Title-only lead",
                    "url": "https://uni.example/jobs/3",
                    "verification_status": "probable",
                    "uncertainty_percent": 60,
                    "uncertainty_flags": ["open_status", "details"],
                },
            ),
        ],
    )
    service = container.get(SearchService)

    result = await service.search(
        SearchBody(
            query="robotics",
            mode="include_probable",
            max_uncertainty=35,
            sort_by="uncertainty",
            sort_order="desc",
        )
    )

    assert [hit.position_id for hit in result.hits] == [2, 1]
    assert [hit.uncertainty_percent for hit in result.hits] == [35, 0]


async def test_legacy_vector_without_status_is_never_silently_verified(container, qdrant):
    await qdrant.create_collection(
        "positions",
        vectors_config=VectorParams(size=4, distance=Distance.COSINE),
    )
    legacy_payload = {
        key: value
        for key, value in PAYLOAD.items()
        if key not in {"verification_status", "confidence"}
    }
    await qdrant.upsert(
        "positions",
        points=[
            PointStruct(
                id=1,
                vector=[1.0, 0.0, 0.0, 0.0],
                payload=legacy_payload,
            )
        ],
    )

    service = container.get(SearchService)
    verified = await service.search(SearchBody(query="robotics"))
    inclusive = await service.search(
        SearchBody(query="robotics", mode="include_probable")
    )

    assert verified.hits == []
    assert [hit.position_id for hit in inclusive.hits] == [1]
    assert inclusive.hits[0].verification_status == "probable"
    assert inclusive.hits[0].confidence is None
    assert inclusive.hits[0].uncertainty_percent == 100
    assert inclusive.hits[0].uncertainty_flags == ["verification"]


async def test_search_country_filter_excludes(container, qdrant):
    await _seed(qdrant)
    service = container.get(SearchService)
    result = await service.search(SearchBody(query="robotics", country="DE"))
    assert result.hits == []


async def test_search_country_filter_accepts_common_italy_aliases(container, qdrant):
    await _seed(qdrant)
    service = container.get(SearchService)
    for alias in ("it", "ITA", "Italy", "Italia"):
        result = await service.search(SearchBody(query="robotics", country=alias))
        assert [hit.position_id for hit in result.hits] == [1]


async def test_search_accepts_multiple_country_and_position_type_filters(container, qdrant):
    await _seed(qdrant)
    service = container.get(SearchService)
    result = await service.search(
        SearchBody(query="robotics", countries=["Italia", "DE"], position_types=["phd"])
    )
    assert [hit.position_id for hit in result.hits] == [1]
    assert result.hits[0].position_type == "phd"


def test_search_body_accepts_internship_filter():
    assert SearchBody(query="design", position_types=["internship"]).position_types == ["internship"]


async def test_search_score_threshold_removes_irrelevant_neighbors(container, qdrant):
    await _seed(qdrant)
    await qdrant.upsert(
        "positions",
        points=[PointStruct(id=2, vector=[0.0, 1.0, 0.0, 0.0], payload={**PAYLOAD, "title": "Unrelated", "url": "https://example/2"})],
    )
    service = container.get(SearchService)
    result = await service.search(SearchBody(query="robotics", min_score=0.5))
    assert [hit.position_id for hit in result.hits] == [1]


async def test_search_returns_related_institutions_from_separate_index(container, qdrant):
    await _seed(qdrant)
    await qdrant.create_collection("positions_institutions", vectors_config=VectorParams(size=4, distance=Distance.COSINE))
    await qdrant.upsert(
        "positions_institutions",
        points=[
            PointStruct(
                id=10,
                vector=[1.0, 0.0, 0.0, 0.0],
                payload={
                    "name": "Design Institute",
                    "kind": "institution",
                    "university": "Design Institute",
                    "country": "CH",
                    "url": "https://design.example",
                    "spontaneous_application_url": "https://design.example/apply",
                    "active_positions": 0,
                },
            )
        ],
    )
    service = container.get(SearchService)
    result = await service.search(SearchBody(query="design"))
    assert result.institutions[0].name == "Design Institute"
    assert result.institutions[0].kind == "institution"
    assert result.institutions[0].active_positions == 0
    assert result.institutions[0].spontaneous_application_url == "https://design.example/apply"


async def test_search_deadline_filter(container, qdrant):
    await _seed(qdrant)
    service = container.get(SearchService)
    kept = await service.search(SearchBody(query="robotics", deadline_after=date(2050, 1, 1)))
    assert [h.position_id for h in kept.hits] == [1]
    dropped = await service.search(SearchBody(query="robotics", deadline_after=date(2100, 1, 1)))
    assert dropped.hits == []


async def test_search_limit_is_optional_and_reports_total(container, qdrant):
    await _seed(qdrant)
    service = container.get(SearchService)
    all_results = await service.search(SearchBody(query="robotics"))
    limited = await service.search(SearchBody(query="robotics", limit=1))
    assert all_results.total == 1
    assert len(all_results.hits) == 1
    assert limited.total == 1
