from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import httpx
import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sqlalchemy.dialects import postgresql

from phd_searcher.config.llm import EmbeddingConfig
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.engine.search_documents import SEARCH_INDEX_CONTRACT_PAYLOAD
from phd_searcher.opportunity_kinds import VACANCY
from scripts.rebuild_search_shadow import (
    CHECKPOINT_SCHEMA_VERSION,
    INSTITUTION_DOCUMENT_CONTRACT,
    POSITION_DOCUMENT_CONTRACT,
    READ_ONLY_SNAPSHOT_SQL,
    SHADOW_BUILD_ID_PAYLOAD,
    SHADOW_CONTRACT_PAYLOAD,
    EmbeddingIdentity,
    _audit_collection_points,
    _build_positions,
    _new_checkpoint,
    _position_candidate_statement,
    _trusted_collection_ids,
    build_parser,
    embedding_config_for_run,
    load_read_only_snapshot,
    migrate_completed_v3_checkpoint,
    position_snapshot_fingerprint,
    resolve_embedding_identity,
    validate_checkpoint_contract,
    validate_collection,
    validate_target_names,
)


@pytest.mark.parametrize(
    ("configured_model", "model_entry", "raw_digest"),
    [
        (
            "ollama/nomic-embed-text",
            {
                "name": "nomic-embed-text:latest",
                "model": "nomic-embed-text:latest",
            },
            "A" * 64,
        ),
        (
            "ollama/qwen3-embedding:0.6b",
            {
                "name": "qwen3-embedding:0.6b",
                "model": "qwen3-embedding:0.6b",
            },
            f"sha256:{'b' * 64}",
        ),
    ],
)
async def test_embedding_identity_normalizes_real_ollama_tag_digests(
    configured_model: str,
    model_entry: dict[str, str],
    raw_digest: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{**model_entry, "digest": raw_digest}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        identity = await resolve_embedding_identity(
            EmbeddingConfig(
                model=configured_model,
                api_base="http://ollama.test:11434/v1",
            ),
            client=client,
        )

    expected_hex = raw_digest.removeprefix("sha256:").lower()
    assert identity.digest == f"sha256:{expected_hex}"
    assert identity.resolved_model == model_entry["model"]


@pytest.mark.parametrize(
    "invalid_digest",
    [None, "", "sha256:too-short", "g" * 64, "sha512:" + "a" * 64],
)
async def test_embedding_identity_rejects_invalid_ollama_digests(
    invalid_digest: object,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "nomic-embed-text:latest",
                        "model": "nomic-embed-text:latest",
                        "digest": invalid_digest,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="sha256 digest"):
            await resolve_embedding_identity(
                EmbeddingConfig(model="ollama/nomic-embed-text"),
                client=client,
            )


def _position(position_id: int = 1) -> Position:
    return Position(
        id=position_id,
        university_id=1,
        listing_page_id=None,
        institution_name=None,
        institution_country=None,
        url=f"https://example.test/jobs/{position_id}",
        title="PhD in interaction design",
        description="Applications are open for a funded doctoral position.",
        area="Human-computer interaction",
        language="en",
        position_type="phd",
        opportunity_kind=VACANCY,
        screening_status="eligible",
        screening_manual=True,
        screening_source="manual",
        screening_decision="eligible",
        screening_confidence=1.0,
        screening_evidence='["Applications are open"]',
        screening_version="manual-v1",
        review_state="resolved",
        is_active=True,
        missing_runs=0,
        scraped_at=datetime(2026, 8, 26, 10, 0),
        indexed_at=datetime(2026, 8, 26, 10, 5),
    )


def _university() -> University:
    return University(
        id=1,
        wikidata_id="Q1",
        name="Example University",
        country="IT",
        website_url="https://example.test",
        catalog_tier="core",
        catalog_basis="test",
        sitelinks=1,
        discovery_status="done",
    )


def test_target_names_never_allow_production_or_implicit_collisions() -> None:
    validate_target_names(
        production_collection="positions",
        collection="positions_shadow_v1",
        institution_collection="institutions_shadow_v1",
    )

    with pytest.raises(ValueError, match="production"):
        validate_target_names(
            production_collection="positions",
            collection="positions",
            institution_collection=None,
        )
    with pytest.raises(ValueError, match="production"):
        validate_target_names(
            production_collection="positions",
            collection="positions_shadow_v1",
            institution_collection="positions_institutions",
        )
    with pytest.raises(ValueError, match="must differ"):
        validate_target_names(
            production_collection="positions",
            collection="same_shadow",
            institution_collection="same_shadow",
        )


def test_shadow_candidate_statement_includes_indexed_and_unindexed_rows() -> None:
    sql = str(
        _position_candidate_statement(date(2026, 8, 26)).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "indexed_at IS NULL" not in sql
    assert "positions.is_active IS true" in sql
    assert "positions.deadline >= '2026-08-26'" in sql
    assert "ORDER BY positions.id" in sql


def test_position_snapshot_hash_ignores_indexed_flag_but_tracks_vector_text() -> None:
    position = _position()
    row = (position, _university(), None)
    original = position_snapshot_fingerprint([row], family_profiles={})

    position.indexed_at = None
    assert position_snapshot_fingerprint([row], family_profiles={}) == original

    position.description = "Different candidate-specific evidence"
    assert position_snapshot_fingerprint([row], family_profiles={}) != original


def test_checkpoint_contract_pins_model_targets_contract_and_snapshot_date() -> None:
    embedding_identity = EmbeddingIdentity(
        configured_model="ollama/nomic-embed-text",
        provider="ollama",
        resolved_model="nomic-embed-text:latest",
        digest="sha256:nomic",
        profile="nomic-search-v1",
    )
    checkpoint = _new_checkpoint(
        collection="position_shadow",
        institution_collection="institution_shadow",
        embedding_identity=embedding_identity,
        embedding_dimension=768,
        as_of=date(2026, 8, 26),
    )

    assert checkpoint["schema_version"] == CHECKPOINT_SCHEMA_VERSION
    assert checkpoint["document_contracts"] == {
        "positions": POSITION_DOCUMENT_CONTRACT,
        "institutions": INSTITUTION_DOCUMENT_CONTRACT,
    }
    assert validate_checkpoint_contract(
        checkpoint,
        collection="position_shadow",
        institution_collection="institution_shadow",
        embedding_identity=embedding_identity,
        embedding_dimension=768,
    ) == date(2026, 8, 26)

    qwen_identity = EmbeddingIdentity(
        configured_model="ollama/qwen3-embedding:0.6b",
        provider="ollama",
        resolved_model="qwen3-embedding:0.6b",
        digest="sha256:qwen",
        profile="qwen3-search-v1",
    )
    with pytest.raises(ValueError, match="embedding"):
        validate_checkpoint_contract(
            checkpoint,
            collection="position_shadow",
            institution_collection="institution_shadow",
            embedding_identity=qwen_identity,
            embedding_dimension=768,
        )


def test_embedding_override_is_process_local_and_retains_connection_config() -> None:
    configured = EmbeddingConfig(
        model="ollama/nomic-embed-text",
        api_base="http://ollama:11434/v1",
        api_key="configured-secret",
    )
    args = build_parser().parse_args(
        [
            "--collection",
            "qwen_shadow",
            "--embedding-model",
            "ollama/qwen3-embedding:4b",
        ]
    )

    selected = embedding_config_for_run(configured, args.embedding_model)

    assert selected.model == "ollama/qwen3-embedding:4b"
    assert selected.api_base == configured.api_base
    assert selected.api_key == configured.api_key
    assert configured.model == "ollama/nomic-embed-text"
    assert selected is not configured


def test_embedding_override_rejects_blank_programmatic_value() -> None:
    configured = EmbeddingConfig(model="ollama/nomic-embed-text")

    with pytest.raises(ValueError, match="cannot be empty"):
        embedding_config_for_run(configured, "   ")


async def test_collection_validation_reports_exact_ids_and_dimension(
    qdrant: AsyncQdrantClient,
) -> None:
    await qdrant.create_collection(
        "shadow",
        vectors_config=VectorParams(size=4, distance=Distance.COSINE),
    )
    await qdrant.upsert(
        "shadow",
        points=[
            PointStruct(id=1, vector=[1.0, 0.0, 0.0, 0.0]),
            PointStruct(id=3, vector=[0.0, 1.0, 0.0, 0.0]),
        ],
        wait=True,
    )

    validation = await validate_collection(
        qdrant,
        collection="shadow",
        expected_ids={1, 2},
    )

    assert not validation.valid
    assert validation.dimension == 4
    assert validation.actual_count == 2
    assert validation.missing_ids == (2,)
    assert validation.unexpected_ids == (3,)


async def test_resume_refuses_foreign_points_but_can_rewrite_recorded_inflight_batch(
    qdrant: AsyncQdrantClient,
) -> None:
    await qdrant.create_collection(
        "shadow_owned",
        vectors_config=VectorParams(size=4, distance=Distance.COSINE),
    )
    await qdrant.upsert(
        "shadow_owned",
        points=[PointStruct(id=1, vector=[1.0, 0.0, 0.0, 0.0])],
        wait=True,
    )

    with pytest.raises(ValueError, match="not owned"):
        await _trusted_collection_ids(
            qdrant,
            collection="shadow_owned",
            build_id="build-a",
            document_contract=POSITION_DOCUMENT_CONTRACT,
            search_index_contract="candidate-contract",
        )

    assert await _trusted_collection_ids(
        qdrant,
        collection="shadow_owned",
        build_id="build-a",
        document_contract=POSITION_DOCUMENT_CONTRACT,
        search_index_contract="candidate-contract",
        allowed_unmarked_ids={1},
    ) == set()

    await qdrant.set_payload(
        "shadow_owned",
        payload={
            SHADOW_BUILD_ID_PAYLOAD: "build-a",
            SHADOW_CONTRACT_PAYLOAD: POSITION_DOCUMENT_CONTRACT,
            "_phdbot_search_index_contract": "candidate-contract",
        },
        points=[1],
        wait=True,
    )
    assert await _trusted_collection_ids(
        qdrant,
        collection="shadow_owned",
        build_id="build-a",
        document_contract=POSITION_DOCUMENT_CONTRACT,
        search_index_contract="candidate-contract",
    ) == {1}


async def test_shadow_position_build_never_mutates_indexed_at(
    qdrant: AsyncQdrantClient,
    tmp_path,
) -> None:
    position = _position()
    original_indexed_at = position.indexed_at
    rows = [(position, _university(), None)]
    checkpoint = _new_checkpoint(
        collection="safe_shadow",
        institution_collection=None,
        embedding_identity=EmbeddingIdentity(
            configured_model="test/embedding",
            provider="test",
            resolved_model="test/embedding",
            digest=None,
            profile="raw-v1",
        ),
        embedding_dimension=4,
        as_of=date(2026, 8, 26),
    )
    checkpoint["positions"] = {
        "fingerprint": "test",
        "expected_count": 1,
        "completed": 0,
        "status": "building",
    }
    model = SimpleNamespace(
        embed_documents=lambda _: None,
        search_index_contract=lambda **_kwargs: (
            f"{POSITION_DOCUMENT_CONTRACT}|raw-v1|test/embedding"
        ),
    )

    async def embed_documents(texts):
        assert texts[0].startswith("Title: PhD in interaction design")
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    model.embed_documents = embed_documents
    validation = await _build_positions(
        qdrant=qdrant,
        model=model,
        collection="safe_shadow",
        rows=rows,
        family_profiles={},
        as_of=date(2026, 8, 26),
        batch_size=64,
        checkpoint=checkpoint,
        checkpoint_path=tmp_path / "checkpoint.json",
    )

    assert validation.valid
    assert position.indexed_at == original_indexed_at
    assert checkpoint["positions"]["status"] == "complete"


async def test_final_validation_reports_wrong_search_contract_for_every_point(
    qdrant: AsyncQdrantClient,
) -> None:
    await qdrant.create_collection(
        "contract_shadow",
        vectors_config=VectorParams(size=4, distance=Distance.COSINE),
    )
    await qdrant.upsert(
        "contract_shadow",
        points=[
            PointStruct(
                id=1,
                vector=[1.0, 0.0, 0.0, 0.0],
                payload={
                    SHADOW_BUILD_ID_PAYLOAD: "build-a",
                    SHADOW_CONTRACT_PAYLOAD: POSITION_DOCUMENT_CONTRACT,
                    SEARCH_INDEX_CONTRACT_PAYLOAD: "stale-contract",
                },
            )
        ],
        wait=True,
    )

    validation = await validate_collection(
        qdrant,
        collection="contract_shadow",
        expected_ids={1},
        build_id="build-a",
        document_contract=POSITION_DOCUMENT_CONTRACT,
        search_index_contract="current-contract",
    )

    assert not validation.valid
    assert validation.search_contract_mismatch_ids == (1,)
    assert validation.ownership_mismatch_ids == ()
    with pytest.raises(ValueError, match="refusing to relabel"):
        await _trusted_collection_ids(
            qdrant,
            collection="contract_shadow",
            build_id="build-a",
            document_contract=POSITION_DOCUMENT_CONTRACT,
            search_index_contract="current-contract",
        )


def _completed_v3_checkpoint(
    *,
    identity: EmbeddingIdentity,
    legacy_document_contract: str,
) -> dict[str, object]:
    checkpoint = _new_checkpoint(
        collection="position_v3",
        institution_collection="institution_v3",
        embedding_identity=identity,
        embedding_dimension=4,
        as_of=date(2026, 8, 26),
    )
    checkpoint["schema_version"] = 3
    checkpoint["document_contract"] = legacy_document_contract
    checkpoint.pop("document_contracts")
    checkpoint["status"] = "complete"
    checkpoint["positions"] = {
        "fingerprint": "positions",
        "expected_count": 1,
        "completed": 1,
        "status": "complete",
        "inflight_ids": [],
    }
    checkpoint["institutions"] = {
        "fingerprint": "institutions",
        "expected_count": 1,
        "completed": 1,
        "status": "complete",
        "inflight_ids": [],
    }
    return checkpoint


async def _create_legacy_contract_collections(
    qdrant: AsyncQdrantClient,
    *,
    build_id: str,
    legacy_document_contract: str,
    position_search_contract: str,
    institution_search_contract: str,
) -> None:
    for collection, search_contract in (
        ("position_v3", position_search_contract),
        ("institution_v3", institution_search_contract),
    ):
        await qdrant.create_collection(
            collection,
            vectors_config=VectorParams(size=4, distance=Distance.COSINE),
        )
        await qdrant.upsert(
            collection,
            points=[
                PointStruct(
                    id=1,
                    vector=[1.0, 0.0, 0.0, 0.0],
                    payload={
                        SHADOW_BUILD_ID_PAYLOAD: build_id,
                        SHADOW_CONTRACT_PAYLOAD: legacy_document_contract,
                        SEARCH_INDEX_CONTRACT_PAYLOAD: search_contract,
                    },
                )
            ],
            wait=True,
        )


async def test_completed_v3_migration_proves_contracts_before_splitting_ownership(
    qdrant: AsyncQdrantClient,
) -> None:
    identity = EmbeddingIdentity(
        configured_model="test/embedding",
        provider="test",
        resolved_model="test/embedding",
        digest=None,
        profile="raw-v1",
    )
    legacy_document_contract = "candidate-compact-legacy"
    checkpoint = _completed_v3_checkpoint(
        identity=identity,
        legacy_document_contract=legacy_document_contract,
    )
    build_id = str(checkpoint["build_id"])
    position_search_contract = f"{POSITION_DOCUMENT_CONTRACT}|raw|test/embedding"
    institution_search_contract = (
        f"{INSTITUTION_DOCUMENT_CONTRACT}|raw|test/embedding"
    )
    model = SimpleNamespace(
        search_index_contract=lambda *, institutions=False: (
            institution_search_contract if institutions else position_search_contract
        )
    )
    await _create_legacy_contract_collections(
        qdrant,
        build_id=build_id,
        legacy_document_contract=legacy_document_contract,
        position_search_contract=position_search_contract,
        institution_search_contract=institution_search_contract,
    )

    migrated_as_of = await migrate_completed_v3_checkpoint(
        checkpoint,
        qdrant=qdrant,
        model=model,
        collection="position_v3",
        institution_collection="institution_v3",
        embedding_identity=identity,
        embedding_dimension=4,
    )

    assert migrated_as_of == date(2026, 8, 26)
    assert checkpoint["schema_version"] == CHECKPOINT_SCHEMA_VERSION
    assert checkpoint["document_contracts"] == {
        "positions": POSITION_DOCUMENT_CONTRACT,
        "institutions": INSTITUTION_DOCUMENT_CONTRACT,
    }
    assert "document_contract" not in checkpoint
    position_audit = await _audit_collection_points(
        qdrant,
        collection="position_v3",
        build_id=build_id,
        document_contract=POSITION_DOCUMENT_CONTRACT,
        search_index_contract=position_search_contract,
    )
    institution_audit = await _audit_collection_points(
        qdrant,
        collection="institution_v3",
        build_id=build_id,
        document_contract=INSTITUTION_DOCUMENT_CONTRACT,
        search_index_contract=institution_search_contract,
    )
    assert not position_audit.ownership_mismatch_ids
    assert not institution_audit.ownership_mismatch_ids


async def test_v3_migration_rejects_unknown_contract_without_relabelling_any_point(
    qdrant: AsyncQdrantClient,
) -> None:
    identity = EmbeddingIdentity(
        configured_model="test/embedding",
        provider="test",
        resolved_model="test/embedding",
        digest=None,
        profile="raw-v1",
    )
    legacy_document_contract = "candidate-compact-legacy"
    checkpoint = _completed_v3_checkpoint(
        identity=identity,
        legacy_document_contract=legacy_document_contract,
    )
    build_id = str(checkpoint["build_id"])
    position_search_contract = f"{POSITION_DOCUMENT_CONTRACT}|raw|test/embedding"
    institution_search_contract = (
        f"{INSTITUTION_DOCUMENT_CONTRACT}|raw|test/embedding"
    )
    model = SimpleNamespace(
        search_index_contract=lambda *, institutions=False: (
            institution_search_contract if institutions else position_search_contract
        )
    )
    await _create_legacy_contract_collections(
        qdrant,
        build_id=build_id,
        legacy_document_contract=legacy_document_contract,
        position_search_contract=position_search_contract,
        institution_search_contract="unknown-contract",
    )

    with pytest.raises(ValueError, match="no payloads were changed"):
        await migrate_completed_v3_checkpoint(
            checkpoint,
            qdrant=qdrant,
            model=model,
            collection="position_v3",
            institution_collection="institution_v3",
            embedding_identity=identity,
            embedding_dimension=4,
        )

    for collection in ("position_v3", "institution_v3"):
        points = await qdrant.retrieve(
            collection_name=collection,
            ids=[1],
            with_payload=True,
            with_vectors=False,
        )
        assert points[0].payload[SHADOW_CONTRACT_PAYLOAD] == legacy_document_contract
    assert checkpoint["schema_version"] == 3


async def test_snapshot_uses_one_repeatable_read_read_only_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.rebuild_search_shadow as shadow

    events: list[str] = []

    class FakeTransaction:
        async def rollback(self) -> None:
            events.append("rollback")

    class FakeConnection:
        async def begin(self) -> FakeTransaction:
            events.append("begin")
            return FakeTransaction()

        async def execute(self, statement) -> None:
            events.append(str(statement))

    connection = FakeConnection()

    class ConnectionContext:
        async def __aenter__(self) -> FakeConnection:
            events.append("connect")
            return connection

        async def __aexit__(self, *_args) -> None:
            events.append("disconnect")

    class FakeEngine:
        def connect(self) -> ConnectionContext:
            return ConnectionContext()

        async def dispose(self) -> None:
            events.append("dispose")

    class FakeSession:
        def __init__(self, *, bind, **_kwargs) -> None:
            assert bind is connection
            events.append("session")

        async def close(self) -> None:
            events.append("session-close")

    async def load_positions(session, *, as_of):
        assert isinstance(session, FakeSession)
        assert as_of == date(2026, 8, 26)
        events.append("positions")
        return ["row"], {"family": "profile"}

    async def load_institutions(session, *, as_of):
        assert isinstance(session, FakeSession)
        assert as_of == date(2026, 8, 26)
        events.append("institutions")
        return [{"id": 1}]

    monkeypatch.setattr(shadow, "create_async_engine", lambda *_args, **_kwargs: FakeEngine())
    monkeypatch.setattr(shadow, "AsyncSession", FakeSession)
    monkeypatch.setattr(shadow, "_load_position_snapshot", load_positions)
    monkeypatch.setattr(shadow, "_load_institution_snapshot", load_institutions)
    settings = SimpleNamespace(
        database=SimpleNamespace(
            url="postgresql+asyncpg://test",
            schema_name="public",
        )
    )

    rows, profiles, entities = await load_read_only_snapshot(
        settings,
        as_of=date(2026, 8, 26),
        include_institutions=True,
    )

    assert rows == ["row"]
    assert profiles == {"family": "profile"}
    assert entities == [{"id": 1}]
    assert events == [
        "connect",
        "begin",
        READ_ONLY_SNAPSHOT_SQL,
        "session",
        "positions",
        "institutions",
        "session-close",
        "rollback",
        "disconnect",
        "dispose",
    ]
