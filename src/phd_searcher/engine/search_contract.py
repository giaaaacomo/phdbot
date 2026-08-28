"""Guards that keep one Qdrant collection on one embedding contract."""

from __future__ import annotations

from typing import Any

from qdrant_client import AsyncQdrantClient

from phd_searcher.engine.search_documents import SEARCH_INDEX_CONTRACT_PAYLOAD


async def validate_search_index_contract(
    qdrant: AsyncQdrantClient,
    collection: str,
    expected: str,
) -> int:
    """Validate every existing point before an incremental writer can mutate it.

    Empty or missing collections are valid new targets.  A legacy, partially
    migrated, or mixed collection fails closed with an actionable shadow-build
    message; no vectors are written or deleted by this function.
    """
    if not await qdrant.collection_exists(collection):
        return 0
    checked = 0
    offset: Any = None
    while True:
        points, offset = await qdrant.scroll(
            collection_name=collection,
            limit=512,
            offset=offset,
            with_payload=[SEARCH_INDEX_CONTRACT_PAYLOAD],
            with_vectors=False,
        )
        for point in points:
            actual = (point.payload or {}).get(SEARCH_INDEX_CONTRACT_PAYLOAD)
            if actual != expected:
                raise RuntimeError(
                    f"search index contract mismatch in {collection!r} at point "
                    f"{point.id}: {actual!r} != {expected!r}; build and validate "
                    "a fresh shadow collection before cutover"
                )
            checked += 1
        if offset is None:
            break
    return checked
