from datetime import date
from unittest.mock import AsyncMock

import pytest
from qdrant_client.models import Distance, PointStruct, VectorParams

from phd_searcher.engine.search_query import MAX_COMBINED_QUERIES, split_combined_query
from phd_searcher.service.search_service import (
    SearchService,
    normalize_retrieval_query,
    normalized_retrieval_queries,
)
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
        ("IxD", "interaction design"),
        ("xray and architecture", "xray and architecture"),
    ],
)
def test_retrieval_query_expands_only_standalone_domain_acronyms(
    query: str,
    normalized: str,
) -> None:
    assert normalize_retrieval_query(query) == normalized


def test_combined_query_splits_single_plus_but_preserves_cplusplus() -> None:
    assert split_combined_query("C++ + VR + interaction design") == [
        "C++",
        "VR",
        "interaction design",
    ]


def test_combined_query_can_escape_a_literal_plus() -> None:
    assert split_combined_query(r"CD4\+ T cells + VR") == ["CD4+ T cells", "VR"]


def test_combined_query_normalizes_and_deduplicates_aliases() -> None:
    assert normalized_retrieval_queries("VR + interaction design + ixd") == [
        "virtual reality",
        "interaction design",
    ]


def test_combined_query_has_a_bounded_number_of_clauses() -> None:
    with pytest.raises(ValueError, match=f"at most {MAX_COMBINED_QUERIES}"):
        SearchBody(query="+".join(f"topic {index}" for index in range(9)))


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


@pytest.mark.parametrize(
    "kwargs", [{}, {"query": " "}, {"universities": [" "]}, {"university": " "}, {"country": "IT"}]
)
def test_browse_requires_an_explicit_institution(kwargs):
    with pytest.raises(ValueError, match="select at least one institution"):
        SearchBody(**kwargs)


def test_browse_normalizes_exact_institutions():
    body = SearchBody(query=" ", university=" Uni Example ", universities=[" A ", "A", "", "B"])
    assert body.query == ""
    assert body.university == "Uni Example"
    assert body.universities == ["A", "B"]


async def test_browse_scrolls_all_pages_without_model_or_relevance(container, qdrant, monkeypatch):
    await _seed(qdrant)
    await qdrant.upsert(
        "positions",
        points=[
            PointStruct(id=i, vector=[0.0, 1.0, 0.0, 0.0], payload={**PAYLOAD, "title": f"PhD {i}"})
            for i in range(2, 301)
        ],
    )
    service = container.get(SearchService)
    embed = AsyncMock(side_effect=AssertionError("Browse must not call the model"))
    monkeypatch.setattr(service._model, "embed_queries", embed)
    query = AsyncMock(side_effect=AssertionError("Browse must not issue vector queries"))
    monkeypatch.setattr(qdrant, "query_points", query)
    result = await service.search(SearchBody(university="Uni Example", min_score=1, limit=2))
    assert result.total == 300
    assert [hit.position_id for hit in result.hits] == [300, 299]
    assert all(hit.score is None for hit in result.hits)
    assert result.institutions == []
    embed.assert_not_awaited()
    query.assert_not_awaited()


async def test_browse_preserves_safety_type_country_and_compensation_filters(container, qdrant):
    await _seed(qdrant)
    changes = {
        2: {"deadline": "2000-01-01", "deadline_ts": "2000-01-01T00:00:00Z"},
        3: {"verification_status": "probable", "uncertainty_percent": 60},
        4: {"university": "Another university"},
        5: {"country": "DE"},
        6: {"position_type": "postdoc"},
        7: {"compensation_max": 10000, "compensation_currency": "EUR"},
        8: {"compensation_max": 60000, "compensation_currency": "EUR"},
        9: {"deadline": None, "deadline_ts": None},
    }
    await qdrant.upsert(
        "positions",
        points=[
            PointStruct(id=i, vector=[0.0, 1.0, 0.0, 0.0], payload={**PAYLOAD, **change})
            for i, change in changes.items()
        ],
    )
    service = container.get(SearchService)
    body = SearchBody(universities=["Uni Example"], countries=["IT"], position_types=["phd"], compensation_min=50000)
    result = await service.search(body)
    # Paid match first; unknown compensation retained, expired/other scopes excluded.
    assert [hit.position_id for hit in result.hits] == [8, 9, 1]
    inclusive = await service.search(body.model_copy(update={"mode": "include_probable", "max_uncertainty": 35}))
    assert [hit.position_id for hit in inclusive.hits] == [8, 9, 1]
    inclusive = await service.search(body.model_copy(update={"mode": "include_probable"}))
    assert [hit.position_id for hit in inclusive.hits] == [8, 9, 3, 1]


async def test_browse_api_returns_nullable_score_and_rejects_unscoped_request(client, qdrant):
    await _seed(qdrant)
    response = await client.post("/v1/search", json={"universities": ["Uni Example"]})
    assert response.status_code == 200
    assert response.json()["hits"][0]["score"] is None
    assert (await client.post("/v1/search", json={"query": ""})).status_code == 422


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
    inclusive = await service.search(SearchBody(query="robotics", mode="include_probable"))

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
    legacy_payload = {key: value for key, value in PAYLOAD.items() if key not in {"verification_status", "confidence"}}
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
    inclusive = await service.search(SearchBody(query="robotics", mode="include_probable"))

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


async def test_search_excludes_explicitly_expired_but_retains_unknown_deadlines(
    container,
    qdrant,
):
    await _seed(qdrant)
    await qdrant.upsert(
        "positions",
        points=[
            PointStruct(
                id=2,
                vector=[1.0, 0.0, 0.0, 0.0],
                payload={
                    **PAYLOAD,
                    "title": "Expired PhD",
                    "url": "https://example/expired",
                    "deadline": "2020-01-01",
                    "deadline_ts": "2020-01-01T00:00:00+00:00",
                },
            ),
            PointStruct(
                id=3,
                vector=[1.0, 0.0, 0.0, 0.0],
                payload={
                    **PAYLOAD,
                    "title": "PhD with unknown deadline",
                    "url": "https://example/unknown",
                    "deadline": None,
                    "deadline_ts": None,
                },
            ),
        ],
    )

    result = await container.get(SearchService).search(SearchBody(query="robotics"))

    assert {hit.position_id for hit in result.hits} == {1, 3}


async def test_search_country_filter_accepts_common_italy_aliases(container, qdrant):
    await _seed(qdrant)
    service = container.get(SearchService)
    for alias in ("it", "ITA", "Italy", "Italia"):
        result = await service.search(SearchBody(query="robotics", country=alias))
        assert [hit.position_id for hit in result.hits] == [1]


async def test_search_accepts_multiple_country_and_position_type_filters(container, qdrant):
    await _seed(qdrant)
    service = container.get(SearchService)
    result = await service.search(SearchBody(query="robotics", countries=["Italia", "DE"], position_types=["phd"]))
    assert [hit.position_id for hit in result.hits] == [1]
    assert result.hits[0].position_type == "phd"


def test_search_body_accepts_internship_filter():
    assert SearchBody(query="design", position_types=["internship"]).position_types == ["internship"]


async def test_search_score_threshold_removes_irrelevant_neighbors(container, qdrant):
    await _seed(qdrant)
    await qdrant.upsert(
        "positions",
        points=[
            PointStruct(
                id=2, vector=[0.0, 1.0, 0.0, 0.0], payload={**PAYLOAD, "title": "Unrelated", "url": "https://example/2"}
            )
        ],
    )
    service = container.get(SearchService)
    result = await service.search(SearchBody(query="robotics", min_score=0.5))
    assert [hit.position_id for hit in result.hits] == [1]


async def test_combined_search_unions_clauses_and_keeps_best_score(container, qdrant, fake_model):
    await qdrant.create_collection("positions", vectors_config=VectorParams(size=4, distance=Distance.COSINE))
    await qdrant.upsert(
        "positions",
        points=[
            PointStruct(id=1, vector=[1.0, 0.0, 0.0, 0.0], payload=PAYLOAD),
            PointStruct(
                id=2,
                vector=[0.0, 1.0, 0.0, 0.0],
                payload={**PAYLOAD, "title": "Interaction role", "url": "https://example/2"},
            ),
            PointStruct(
                id=3,
                vector=[0.8, 0.6, 0.0, 0.0],
                payload={**PAYLOAD, "title": "Mixed role", "url": "https://example/3"},
            ),
        ],
    )
    fake_model.embed_queries = AsyncMock(return_value=[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
    service = container.get(SearchService)

    result = await service.search(SearchBody(query="VR + interaction design + ixd", min_score=0.5))

    fake_model.embed_queries.assert_awaited_once_with(["virtual reality", "interaction design"])
    assert [hit.position_id for hit in result.hits] == [1, 2, 3]
    assert result.hits[2].score == pytest.approx(0.8)
    assert result.total == 3


async def test_search_returns_related_institutions_from_separate_index(container, qdrant):
    await _seed(qdrant)
    await qdrant.create_collection(
        "positions_institutions", vectors_config=VectorParams(size=4, distance=Distance.COSINE)
    )
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


async def test_nullable_filters_keep_unknown_values_after_known_matches(container, qdrant):
    await qdrant.create_collection("positions", vectors_config=VectorParams(size=4, distance=Distance.COSINE))
    await qdrant.upsert(
        "positions",
        points=[
            PointStruct(
                id=1,
                vector=[0.9, 0.43589, 0.0, 0.0],
                payload={
                    **PAYLOAD,
                    "title": "Known matching pay",
                    "compensation_min": 60000,
                    "compensation_max": 70000,
                    "compensation_currency": "EUR",
                },
            ),
            PointStruct(
                id=2,
                vector=[1.0, 0.0, 0.0, 0.0],
                payload={
                    **PAYLOAD,
                    "title": "Unknown pay and deadline",
                    "url": "https://example/2",
                    "deadline": None,
                    "deadline_ts": None,
                },
            ),
            PointStruct(
                id=3,
                vector=[0.8, 0.6, 0.0, 0.0],
                payload={
                    **PAYLOAD,
                    "title": "Known pay below threshold",
                    "url": "https://example/3",
                    "compensation_min": 20000,
                    "compensation_max": 25000,
                    "compensation_currency": "EUR",
                },
            ),
        ],
    )
    service = container.get(SearchService)

    result = await service.search(
        SearchBody(
            query="robotics",
            compensation_min=50000,
            deadline_after=date(2050, 1, 1),
        )
    )

    assert [hit.position_id for hit in result.hits] == [1, 2]
    assert result.hits[1].compensation_eur_max is None
    assert result.hits[1].deadline is None
    assert result.total == 2


async def test_date_filter_keeps_unknown_dates_in_relevance_order(container, qdrant):
    await qdrant.create_collection("positions", vectors_config=VectorParams(size=4, distance=Distance.COSINE))
    await qdrant.upsert(
        "positions",
        points=[
            PointStruct(
                id=1,
                vector=[0.9, 0.43589, 0.0, 0.0],
                payload={**PAYLOAD, "title": "Known matching deadline"},
            ),
            PointStruct(
                id=2,
                vector=[1.0, 0.0, 0.0, 0.0],
                payload={
                    **PAYLOAD,
                    "title": "Unknown deadline",
                    "url": "https://example/2",
                    "deadline": None,
                    "deadline_ts": None,
                },
            ),
        ],
    )
    service = container.get(SearchService)

    result = await service.search(SearchBody(query="robotics", deadline_after=date(2050, 1, 1)))

    assert [hit.position_id for hit in result.hits] == [2, 1]


async def test_search_limit_is_optional_and_reports_total(container, qdrant):
    await _seed(qdrant)
    service = container.get(SearchService)
    all_results = await service.search(SearchBody(query="robotics"))
    limited = await service.search(SearchBody(query="robotics", limit=1))
    assert all_results.total == 1
    assert len(all_results.hits) == 1
    assert limited.total == 1
