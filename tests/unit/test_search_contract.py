from __future__ import annotations

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from phd_searcher.engine.search_contract import validate_search_index_contract
from phd_searcher.engine.search_documents import SEARCH_INDEX_CONTRACT_PAYLOAD


async def test_contract_guard_accepts_consistent_collection(
    qdrant: AsyncQdrantClient,
) -> None:
    expected = "candidate-compact-v2|nomic-v1|ollama/nomic-embed-text"
    await qdrant.create_collection(
        "consistent",
        vectors_config=VectorParams(size=2, distance=Distance.COSINE),
    )
    await qdrant.upsert(
        "consistent",
        points=[
            PointStruct(
                id=1,
                vector=[1.0, 0.0],
                payload={SEARCH_INDEX_CONTRACT_PAYLOAD: expected},
            ),
            PointStruct(
                id=2,
                vector=[0.0, 1.0],
                payload={SEARCH_INDEX_CONTRACT_PAYLOAD: expected},
            ),
        ],
    )

    assert await validate_search_index_contract(
        qdrant,
        "consistent",
        expected,
    ) == 2


@pytest.mark.parametrize("actual", [None, "legacy", "candidate-compact-v1|qwen-v1|other"])
async def test_contract_guard_rejects_legacy_or_mixed_collection(
    qdrant: AsyncQdrantClient,
    actual: str | None,
) -> None:
    await qdrant.create_collection(
        "mismatch",
        vectors_config=VectorParams(size=2, distance=Distance.COSINE),
    )
    payload = {} if actual is None else {SEARCH_INDEX_CONTRACT_PAYLOAD: actual}
    await qdrant.upsert(
        "mismatch",
        points=[PointStruct(id=1, vector=[1.0, 0.0], payload=payload)],
    )

    with pytest.raises(RuntimeError, match="fresh shadow collection"):
        await validate_search_index_contract(
            qdrant,
            "mismatch",
            "candidate-compact-v2|nomic-v1|ollama/nomic-embed-text",
        )


async def test_contract_guard_allows_missing_or_empty_target(
    qdrant: AsyncQdrantClient,
) -> None:
    assert await validate_search_index_contract(qdrant, "missing", "expected") == 0
    await qdrant.create_collection(
        "empty",
        vectors_config=VectorParams(size=2, distance=Distance.COSINE),
    )
    assert await validate_search_index_contract(qdrant, "empty", "expected") == 0
