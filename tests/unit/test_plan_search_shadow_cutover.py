from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from phd_searcher.config import Settings
from phd_searcher.config.database import DatabaseConfig
from phd_searcher.config.llm import EmbeddingConfig, LLMConfig
from phd_searcher.config.qdrant import QdrantConfig
from phd_searcher.engine.search_documents import (
    CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
    INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
    SEARCH_INDEX_CONTRACT_PAYLOAD,
)
from scripts.plan_search_shadow_cutover import (
    READ_ONLY_SNAPSHOT_SQL,
    SHADOW_BUILD_ID_PAYLOAD,
    SHADOW_CONTRACT_PAYLOAD,
    CollectionAudit,
    DatabaseSnapshot,
    ModelIdentity,
    _optimizer_health,
    audit_collection,
    build_cutover_report,
    build_parser,
    id_set_sha256,
    load_database_snapshot,
    resolve_model_identity,
)

POSITION_CONTRACT = (
    f"{CANDIDATE_SEARCH_DOCUMENT_CONTRACT}|nomic-v1|ollama/nomic-embed-text"
)
INSTITUTION_CONTRACT = (
    f"{INSTITUTION_SEARCH_DOCUMENT_CONTRACT}|nomic-v1|ollama/nomic-embed-text"
)
BUILD_ID = "build-123"
DIGEST = f"sha256:{'a' * 64}"


def _identity() -> ModelIdentity:
    return ModelIdentity(
        configured_model="ollama/nomic-embed-text",
        provider="ollama",
        resolved_model="nomic-embed-text:latest",
        digest=DIGEST,
        profile="nomic",
        manifest_dimension=768,
    )


def _snapshot() -> DatabaseSnapshot:
    return DatabaseSnapshot(
        as_of=date(2026, 8, 27),
        position_ids=frozenset({1, 2}),
        institution_ids=frozenset({10}),
        indexed_position_ids=frozenset({1, 99}),
        position_fingerprint="positions-fingerprint",
        institution_fingerprint="institutions-fingerprint",
    )


def _audit(
    *,
    collection: str,
    ids: set[int],
    contract: str,
    document_contract: str,
) -> CollectionAudit:
    count = len(ids)
    return CollectionAudit(
        collection=collection,
        exists=True,
        exact_count=count,
        dimension=768,
        integer_ids=frozenset(ids),
        non_integer_ids=(),
        search_contracts=((contract, count),),
        build_ids=((BUILD_ID, count),),
        shadow_contracts=((document_contract, count),),
        status="green",
        optimizer_status="ok",
        distance=Distance.COSINE.value,
    )


def _checkpoint() -> dict[str, object]:
    return {
        "schema_version": 4,
        "status": "complete",
        "as_of": "2026-08-27",
        "build_id": BUILD_ID,
        "collection": "position_shadow",
        "institution_collection": "institution_shadow",
        "document_contracts": {
            "positions": CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
            "institutions": INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
        },
        "embedding_profile": "nomic",
        "embedding": {
            "configured_model": "ollama/nomic-embed-text",
            "provider": "ollama",
            "resolved_model": "nomic-embed-text:latest",
            "digest": DIGEST,
        },
        "embedding_dimension": 768,
        "positions": {
            "status": "complete",
            "expected_count": 2,
            "completed": 2,
            "fingerprint": "positions-fingerprint",
        },
        "institutions": {
            "status": "complete",
            "expected_count": 1,
            "completed": 1,
            "fingerprint": "institutions-fingerprint",
        },
    }


def test_ready_plan_contains_exact_reversible_database_deltas() -> None:
    report = build_cutover_report(
        production_collection="positions",
        collection="position_shadow",
        institution_collection="institution_shadow",
        model_identity=_identity(),
        position_contract=POSITION_CONTRACT,
        institution_contract=INSTITUTION_CONTRACT,
        database=_snapshot(),
        positions=_audit(
            collection="position_shadow",
            ids={1, 2},
            contract=POSITION_CONTRACT,
            document_contract=CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
        ),
        institutions=_audit(
            collection="institution_shadow",
            ids={10},
            contract=INSTITUTION_CONTRACT,
            document_contract=INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
        ),
        checkpoint_file=Path("checkpoint.json"),
        checkpoint=_checkpoint(),
        checkpoint_error=None,
    )

    assert report["read_only"] is True
    assert report["ready_for_atomic_cutover"] is True
    assert report["blockers"] == []
    reconciliation = report["reconciliation"]
    assert reconciliation["positions"]["db_indexed_at_to_set_ids"] == [2]
    assert reconciliation["positions"]["db_indexed_at_to_clear_ids"] == [99]
    assert report["cutover_manifest"]["safe_position_ids"] == [1, 2]
    assert report["cutover_manifest"]["backup_required_before_mutation"] is True
    assert report["database_snapshot"]["safe_position_ids_sha256"] == id_set_sha256(
        {1, 2}
    )


def test_plan_reports_membership_and_every_point_contract_blockers() -> None:
    bad_positions = CollectionAudit(
        collection="position_shadow",
        exists=True,
        exact_count=2,
        dimension=768,
        integer_ids=frozenset({1, 3}),
        non_integer_ids=(),
        search_contracts=((None, 1), (POSITION_CONTRACT, 1)),
        build_ids=((BUILD_ID, 2),),
        shadow_contracts=((CANDIDATE_SEARCH_DOCUMENT_CONTRACT, 2),),
        status="green",
        optimizer_status="ok",
        distance=Distance.COSINE.value,
    )
    report = build_cutover_report(
        production_collection="positions",
        collection="position_shadow",
        institution_collection="institution_shadow",
        model_identity=_identity(),
        position_contract=POSITION_CONTRACT,
        institution_contract=INSTITUTION_CONTRACT,
        database=_snapshot(),
        positions=bad_positions,
        institutions=_audit(
            collection="institution_shadow",
            ids={10},
            contract=INSTITUTION_CONTRACT,
            document_contract=INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
        ),
        checkpoint_file=Path("checkpoint.json"),
        checkpoint=_checkpoint(),
        checkpoint_error=None,
    )

    codes = {blocker["code"] for blocker in report["blockers"]}
    assert report["ready_for_atomic_cutover"] is False
    assert "positions_membership_mismatch" in codes
    assert "positions_search_contract_mismatch" in codes
    assert report["reconciliation"]["positions"]["safe_missing_from_shadow_ids"] == [2]
    assert report["reconciliation"]["positions"]["shadow_not_safe_ids"] == [3]


async def test_collection_audit_scans_exact_ids_dimension_and_contracts(
    qdrant: AsyncQdrantClient,
) -> None:
    await qdrant.create_collection(
        "shadow",
        vectors_config=VectorParams(size=4, distance=Distance.COSINE),
    )
    await qdrant.upsert(
        "shadow",
        points=[
            PointStruct(
                id=1,
                vector=[1.0, 0.0, 0.0, 0.0],
                payload={
                    SEARCH_INDEX_CONTRACT_PAYLOAD: POSITION_CONTRACT,
                    SHADOW_BUILD_ID_PAYLOAD: BUILD_ID,
                    SHADOW_CONTRACT_PAYLOAD: CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
                },
            ),
            PointStruct(
                id=2,
                vector=[0.0, 1.0, 0.0, 0.0],
                payload={
                    SEARCH_INDEX_CONTRACT_PAYLOAD: POSITION_CONTRACT,
                    SHADOW_BUILD_ID_PAYLOAD: BUILD_ID,
                    SHADOW_CONTRACT_PAYLOAD: CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
                },
            ),
        ],
        wait=True,
    )

    audit = await audit_collection(qdrant, "shadow")

    assert audit.exact_count == 2
    assert audit.dimension == 4
    assert audit.integer_ids == {1, 2}
    assert audit.search_contracts == ((POSITION_CONTRACT, 2),)
    assert audit.build_ids == ((BUILD_ID, 2),)
    assert audit.status == "green"
    assert audit.optimizer_status == "ok"
    assert audit.optimizer_error is None
    assert audit.distance == Distance.COSINE.value


@pytest.mark.parametrize(
    ("checkpoint_update", "expected_code"),
    [
        ({"schema_version": 3}, "checkpoint_schema_version_mismatch"),
        ({"build_id": "  "}, "checkpoint_build_id_invalid"),
        ({"as_of": "2026-08-26"}, "checkpoint_as_of_mismatch"),
    ],
)
def test_plan_blocks_invalid_checkpoint_identity(
    checkpoint_update: dict[str, object],
    expected_code: str,
) -> None:
    checkpoint = {**_checkpoint(), **checkpoint_update}
    report = build_cutover_report(
        production_collection="positions",
        collection="position_shadow",
        institution_collection="institution_shadow",
        model_identity=_identity(),
        position_contract=POSITION_CONTRACT,
        institution_contract=INSTITUTION_CONTRACT,
        database=_snapshot(),
        positions=_audit(
            collection="position_shadow",
            ids={1, 2},
            contract=POSITION_CONTRACT,
            document_contract=CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
        ),
        institutions=_audit(
            collection="institution_shadow",
            ids={10},
            contract=INSTITUTION_CONTRACT,
            document_contract=INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
        ),
        checkpoint_file=Path("checkpoint.json"),
        checkpoint=checkpoint,
        checkpoint_error=None,
    )

    codes = {blocker["code"] for blocker in report["blockers"]}
    assert report["ready_for_atomic_cutover"] is False
    assert expected_code in codes


@pytest.mark.parametrize(
    ("audit_update", "expected_code"),
    [
        ({"status": "yellow"}, "positions_collection_not_green"),
        ({"optimizer_status": "error", "optimizer_error": "failed"}, "positions_optimizer_not_ok"),
        ({"distance": Distance.DOT.value}, "positions_distance_mismatch"),
    ],
)
def test_plan_blocks_unhealthy_or_incompatible_collection(
    audit_update: dict[str, object],
    expected_code: str,
) -> None:
    positions = replace(
        _audit(
            collection="position_shadow",
            ids={1, 2},
            contract=POSITION_CONTRACT,
            document_contract=CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
        ),
        **audit_update,
    )
    report = build_cutover_report(
        production_collection="positions",
        collection="position_shadow",
        institution_collection="institution_shadow",
        model_identity=_identity(),
        position_contract=POSITION_CONTRACT,
        institution_contract=INSTITUTION_CONTRACT,
        database=_snapshot(),
        positions=positions,
        institutions=_audit(
            collection="institution_shadow",
            ids={10},
            contract=INSTITUTION_CONTRACT,
            document_contract=INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
        ),
        checkpoint_file=Path("checkpoint.json"),
        checkpoint=_checkpoint(),
        checkpoint_error=None,
    )

    codes = {blocker["code"] for blocker in report["blockers"]}
    assert report["ready_for_atomic_cutover"] is False
    assert expected_code in codes


def test_optimizer_error_variant_is_not_treated_as_unexposed() -> None:
    info = SimpleNamespace(
        optimizer_status=SimpleNamespace(error="optimizer failed")
    )

    assert _optimizer_health(info) == ("error", "optimizer failed")


async def test_model_identity_uses_ollama_digest_and_manifest_dimension() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "nomic-embed-text:latest",
                            "model": "nomic-embed-text:latest",
                            "digest": "A" * 64,
                        }
                    ]
                },
            )
        assert request.url.path == "/api/show"
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={"model_info": {"nomic_bert.embedding_length": 768}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        identity = await resolve_model_identity(
            EmbeddingConfig(
                model="ollama/nomic-embed-text",
                api_base="http://ollama.test:11434/v1",
            ),
            client=client,
        )

    assert identity.digest == DIGEST
    assert identity.manifest_dimension == 768
    assert identity.resolved_model == "nomic-embed-text:latest"


async def test_database_snapshot_sets_read_only_isolation_before_materializing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    expected = _snapshot()

    class FakeTransaction:
        async def rollback(self) -> None:
            calls.append("rollback")

    class FakeConnection:
        async def begin(self) -> FakeTransaction:
            calls.append("begin")
            return FakeTransaction()

        async def execute(self, statement: object) -> None:
            calls.append(str(statement))

    connection = FakeConnection()

    class FakeConnectionContext:
        async def __aenter__(self) -> FakeConnection:
            return connection

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeEngine:
        def connect(self) -> FakeConnectionContext:
            return FakeConnectionContext()

        async def dispose(self) -> None:
            calls.append("dispose")

    class FakeSession:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def close(self) -> None:
            calls.append("session.close")

    async def fake_materialize(
        _session: object,
        *,
        as_of: date,
    ) -> DatabaseSnapshot:
        assert as_of == date(2026, 8, 27)
        calls.append("materialize")
        return expected

    monkeypatch.setattr(
        "scripts.plan_search_shadow_cutover.create_async_engine",
        lambda *_args, **_kwargs: FakeEngine(),
    )
    monkeypatch.setattr(
        "scripts.plan_search_shadow_cutover.AsyncSession",
        FakeSession,
    )
    monkeypatch.setattr(
        "scripts.plan_search_shadow_cutover._materialize_database_snapshot",
        fake_materialize,
    )
    settings = Settings(
        llm=LLMConfig(model="test/model"),
        embedding=EmbeddingConfig(model="test/embedding"),
        database=DatabaseConfig(url="postgresql+asyncpg://u:p@localhost/test"),
        qdrant=QdrantConfig(),
    )

    result = await load_database_snapshot(settings, as_of=date(2026, 8, 27))

    assert result is expected
    assert calls[:3] == ["begin", READ_ONLY_SNAPSHOT_SQL, "materialize"]
    assert calls[-3:] == ["session.close", "rollback", "dispose"]


def test_parser_requires_both_collections_and_exact_model() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--collection",
            "position_shadow",
            "--institution-collection",
            "institution_shadow",
            "--embedding-model",
            "ollama/nomic-embed-text",
        ]
    )
    assert args.collection == "position_shadow"
    assert args.institution_collection == "institution_shadow"
    assert args.embedding_model == "ollama/nomic-embed-text"

    with pytest.raises(SystemExit):
        parser.parse_args(["--collection", "position_shadow"])
