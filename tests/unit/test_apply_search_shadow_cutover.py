from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from qdrant_client.models import Distance

from phd_searcher.config import Settings
from phd_searcher.config.database import DatabaseConfig
from phd_searcher.config.llm import EmbeddingConfig, LLMConfig
from phd_searcher.config.qdrant import QdrantConfig
from phd_searcher.engine.search_documents import (
    CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
    INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
)
from scripts.apply_search_shadow_cutover import (
    CutoverExpectations,
    CutoverRefused,
    apply_cutover,
    validate_database_snapshot,
    validate_manifest,
    validate_production_audits,
    validate_shadow_audit,
    verify_backup,
)
from scripts.plan_search_shadow_cutover import (
    CollectionAudit,
    DatabaseSnapshot,
    ModelIdentity,
    build_cutover_report,
    id_set_sha256,
)

POSITION_COLLECTION = "positions_shadow_nomic_compact_v2"
INSTITUTION_COLLECTION = f"{POSITION_COLLECTION}_institutions"
PRODUCTION_COLLECTION = "positions"
MODEL = "ollama/nomic-embed-text"
DIGEST = f"sha256:{'a' * 64}"
BUILD_ID = "build-123"
POSITION_CONTRACT = (
    f"{CANDIDATE_SEARCH_DOCUMENT_CONTRACT}|nomic-v1|{MODEL}"
)
INSTITUTION_CONTRACT = (
    f"{INSTITUTION_SEARCH_DOCUMENT_CONTRACT}|nomic-v1|{MODEL}"
)
AS_OF = date(2026, 8, 28)


def _expectations() -> CutoverExpectations:
    return CutoverExpectations(
        production_collection=PRODUCTION_COLLECTION,
        position_collection=POSITION_COLLECTION,
        institution_collection=INSTITUTION_COLLECTION,
        embedding_model=MODEL,
        embedding_digest=DIGEST,
    )


def _snapshot() -> DatabaseSnapshot:
    return DatabaseSnapshot(
        as_of=AS_OF,
        position_ids=frozenset({1, 2}),
        institution_ids=frozenset({10}),
        indexed_position_ids=frozenset({1, 99}),
        position_fingerprint="b" * 64,
        institution_fingerprint="c" * 64,
    )


def _audit(
    collection: str,
    ids: set[int],
    *,
    contract: str,
    document_contract: str,
    build_id: str = BUILD_ID,
) -> CollectionAudit:
    count = len(ids)
    return CollectionAudit(
        collection=collection,
        exists=True,
        exact_count=count,
        dimension=4,
        integer_ids=frozenset(ids),
        non_integer_ids=(),
        search_contracts=((contract, count),),
        build_ids=((build_id, count),),
        shadow_contracts=((document_contract, count),),
        status="green",
        optimizer_status="ok",
        optimizer_error=None,
        distance=Distance.COSINE.value,
    )


def _checkpoint() -> dict[str, object]:
    snapshot = _snapshot()
    return {
        "schema_version": 4,
        "status": "complete",
        "as_of": AS_OF.isoformat(),
        "build_id": BUILD_ID,
        "collection": POSITION_COLLECTION,
        "institution_collection": INSTITUTION_COLLECTION,
        "document_contracts": {
            "positions": CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
            "institutions": INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
        },
        "embedding_profile": "nomic",
        "embedding": {
            "configured_model": MODEL,
            "provider": "ollama",
            "resolved_model": "nomic-embed-text:latest",
            "digest": DIGEST,
        },
        "embedding_dimension": 4,
        "positions": {
            "status": "complete",
            "expected_count": 2,
            "completed": 2,
            "fingerprint": snapshot.position_fingerprint,
        },
        "institutions": {
            "status": "complete",
            "expected_count": 1,
            "completed": 1,
            "fingerprint": snapshot.institution_fingerprint,
        },
    }


def _manifest() -> dict[str, object]:
    identity = ModelIdentity(
        configured_model=MODEL,
        provider="ollama",
        resolved_model="nomic-embed-text:latest",
        digest=DIGEST,
        profile="nomic",
        manifest_dimension=4,
    )
    report = build_cutover_report(
        production_collection=PRODUCTION_COLLECTION,
        collection=POSITION_COLLECTION,
        institution_collection=INSTITUTION_COLLECTION,
        model_identity=identity,
        position_contract=POSITION_CONTRACT,
        institution_contract=INSTITUTION_CONTRACT,
        database=_snapshot(),
        positions=_audit(
            POSITION_COLLECTION,
            {1, 2},
            contract=POSITION_CONTRACT,
            document_contract=CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
        ),
        institutions=_audit(
            INSTITUTION_COLLECTION,
            {10},
            contract=INSTITUTION_CONTRACT,
            document_contract=INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
        ),
        checkpoint_file=Path("checkpoint.json"),
        checkpoint=_checkpoint(),
        checkpoint_error=None,
    )
    report["generated_at"] = datetime.now(UTC).isoformat()
    assert report["ready_for_atomic_cutover"] is True
    return report


def _validated_manifest():
    return validate_manifest(_manifest(), _expectations())


def _settings(*, collection: str = PRODUCTION_COLLECTION) -> Settings:
    return Settings(
        llm=LLMConfig(model="ollama/test"),
        embedding=EmbeddingConfig(model=MODEL, api_base="http://ollama.test/v1"),
        database=DatabaseConfig(url="postgresql+asyncpg://u:p@db/test"),
        qdrant=QdrantConfig(url="http://qdrant.test", collection=collection),
    )


def test_valid_manifest_reconstructs_exact_old_and_target_memberships() -> None:
    plan = _validated_manifest()

    validate_database_snapshot(_snapshot(), plan)
    assert plan.safe_position_ids == {1, 2}
    assert plan.to_set == {2}
    assert plan.to_clear == {99}
    assert plan.indexed_membership_sha256 == id_set_sha256({1, 99})


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), 99),
        (("ready_for_atomic_cutover",), False),
        (("checkpoint", "schema_version"), 3),
        (("checkpoint", "build_id"), ""),
        (("checkpoint", "as_of"), "2026-08-27"),
        (("checkpoint", "embedding_profile"), "raw"),
        (("collections", "positions", "status"), "yellow"),
        (("collections", "positions", "optimizer_status"), "error"),
        (("collections", "positions", "distance"), Distance.DOT.value),
    ],
)
def test_manifest_validation_fails_closed_on_tampered_contracts(
    path: tuple[str, ...],
    value: object,
) -> None:
    payload = copy.deepcopy(_manifest())
    target = payload
    for key in path[:-1]:
        target = target[key]  # type: ignore[index,assignment]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(CutoverRefused):
        validate_manifest(payload, _expectations())


def test_manifest_rejects_deltas_that_do_not_reconstruct_snapshot() -> None:
    payload = copy.deepcopy(_manifest())
    manifest = payload["cutover_manifest"]
    reconciliation = payload["reconciliation"]
    assert isinstance(manifest, dict)
    assert isinstance(reconciliation, dict)
    positions = reconciliation["positions"]
    assert isinstance(positions, dict)
    manifest["db_indexed_at_to_set_ids"] = []
    positions["db_indexed_at_to_set_ids"] = []

    with pytest.raises(CutoverRefused, match="reconstruct"):
        validate_manifest(payload, _expectations())


@pytest.mark.parametrize(
    "current",
    [
        replace(_snapshot(), indexed_position_ids=frozenset({1})),
        replace(_snapshot(), position_fingerprint="d" * 64),
        replace(_snapshot(), institution_ids=frozenset({11})),
        replace(_snapshot(), as_of=date(2026, 8, 27)),
    ],
)
def test_database_snapshot_validation_rejects_any_drift(
    current: DatabaseSnapshot,
) -> None:
    with pytest.raises(CutoverRefused):
        validate_database_snapshot(current, _validated_manifest())


@pytest.mark.parametrize(
    "changed",
    [
        {"status": "yellow"},
        {"optimizer_status": "error", "optimizer_error": "failed"},
        {"distance": Distance.DOT.value},
        {"integer_ids": frozenset({1, 3})},
        {"build_ids": (("other-build", 2),)},
    ],
)
def test_shadow_audit_rejects_health_contract_or_membership_drift(
    changed: dict[str, object],
) -> None:
    healthy = _audit(
        POSITION_COLLECTION,
        {1, 2},
        contract=POSITION_CONTRACT,
        document_contract=CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
    )
    validate_shadow_audit(
        healthy,
        collection=POSITION_COLLECTION,
        ids=frozenset({1, 2}),
        dimension=4,
        search_contract=POSITION_CONTRACT,
        build_id=BUILD_ID,
        document_contract=CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
    )
    audit = replace(healthy, **changed)

    with pytest.raises(CutoverRefused):
        validate_shadow_audit(
            audit,
            collection=POSITION_COLLECTION,
            ids=frozenset({1, 2}),
            dimension=4,
            search_contract=POSITION_CONTRACT,
            build_id=BUILD_ID,
            document_contract=CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
        )


def test_production_audit_requires_healthy_exact_rollback_collection() -> None:
    positions = _audit(
        PRODUCTION_COLLECTION,
        {1, 99},
        contract="legacy",
        document_contract="legacy",
    )
    institutions = _audit(
        f"{PRODUCTION_COLLECTION}_institutions",
        {10},
        contract="legacy-institutions",
        document_contract="legacy-institutions",
    )
    validate_production_audits(
        positions,
        institutions,
        production_collection=PRODUCTION_COLLECTION,
        indexed_ids=frozenset({1, 99}),
    )

    with pytest.raises(CutoverRefused):
        validate_production_audits(
            replace(positions, status="red"),
            institutions,
            production_collection=PRODUCTION_COLLECTION,
            indexed_ids=frozenset({1, 99}),
        )
    with pytest.raises(CutoverRefused):
        validate_production_audits(
            positions,
            institutions,
            production_collection=PRODUCTION_COLLECTION,
            indexed_ids=frozenset({1}),
        )


def test_backup_verification_uses_listable_custom_dump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = tmp_path / "fresh.dump"
    backup.write_bytes(b"PGDMP-valid-test-content")
    command_seen: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        "scripts.apply_search_shadow_cutover.shutil.which",
        lambda name: "/mock/pg_restore" if name == "pg_restore" else None,
    )

    def fake_run(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        command_seen.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b"; header\n1; TABLE public positions\n",
            stderr=b"",
        )

    monkeypatch.setattr(
        "scripts.apply_search_shadow_cutover.subprocess.run",
        fake_run,
    )

    evidence = verify_backup(
        backup,
        generated_at=datetime.now(UTC) - timedelta(minutes=1),
        max_age=timedelta(hours=1),
    )

    assert evidence.listing_entries == 1
    assert evidence.sha256.startswith("sha256:")
    assert command_seen == [("/mock/pg_restore", "--list", str(backup.resolve()))]


def test_backup_verification_rejects_failed_mocked_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = tmp_path / "fresh.dump"
    backup.write_bytes(b"PGDMP-valid-test-content")
    monkeypatch.setattr(
        "scripts.apply_search_shadow_cutover.shutil.which",
        lambda name: "/mock/pg_restore" if name == "pg_restore" else None,
    )
    monkeypatch.setattr(
        "scripts.apply_search_shadow_cutover.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout=b"",
            stderr=b"broken dump",
        ),
    )

    with pytest.raises(CutoverRefused, match="could not list"):
        verify_backup(
            backup,
            generated_at=datetime.now(UTC) - timedelta(minutes=1),
            max_age=timedelta(hours=1),
        )


async def test_apply_refuses_config_drift_before_external_checks_or_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(_manifest()),
        encoding="utf-8",
    )
    api_check = AsyncMock()
    monkeypatch.setattr(
        "scripts.apply_search_shadow_cutover._require_api_down",
        api_check,
    )
    monkeypatch.setattr(
        "scripts.apply_search_shadow_cutover.local_today",
        lambda: AS_OF,
    )

    with pytest.raises(CutoverRefused, match="production collection changed"):
        await apply_cutover(
            settings=_settings(collection="other-production"),
            manifest_path=manifest,
            expectations=_expectations(),
            backup_path=tmp_path / "unused.dump",
            recovery_path=tmp_path / "recovery.json",
            max_backup_age=timedelta(hours=1),
            api_health_url="http://api.test/health",
        )

    api_check.assert_not_awaited()
    assert not (tmp_path / "recovery.json").exists()


async def test_apply_refuses_reachable_api_before_backup_or_database_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(_manifest()),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.apply_search_shadow_cutover.local_today",
        lambda: AS_OF,
    )
    monkeypatch.setattr(
        "scripts.apply_search_shadow_cutover._require_api_down",
        AsyncMock(side_effect=CutoverRefused("API is still reachable")),
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("backup/database work must not start while API is up")

    monkeypatch.setattr(
        "scripts.apply_search_shadow_cutover.verify_backup",
        forbidden,
    )
    monkeypatch.setattr(
        "scripts.apply_search_shadow_cutover.create_async_engine",
        forbidden,
    )

    with pytest.raises(CutoverRefused, match="API is still reachable"):
        await apply_cutover(
            settings=_settings(),
            manifest_path=manifest,
            expectations=_expectations(),
            backup_path=tmp_path / "unused.dump",
            recovery_path=tmp_path / "recovery.json",
            max_backup_age=timedelta(hours=1),
            api_health_url="http://api.test/health",
        )

    assert not (tmp_path / "recovery.json").exists()
