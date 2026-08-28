"""Read-only preflight for activating a completed PHDBOT search shadow.

The command does not update PostgreSQL, Qdrant, ``.env``, aliases, or files. It
materializes the current searchable universe in one PostgreSQL
``REPEATABLE READ, READ ONLY`` transaction, scans every point in both proposed
Qdrant collections, and emits a JSON manifest. A later, separately reviewed
cutover can consume the exact ID deltas in that manifest after taking a backup.

Example::

    uv run python scripts/plan_search_shadow_cutover.py \
      --collection positions_shadow_nomic_compact_v1 \
      --institution-collection positions_shadow_nomic_compact_v1_institutions \
      --embedding-model ollama/nomic-embed-text
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from dotenv import load_dotenv
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import CollectionInfo, Distance
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import Select

from phd_searcher.clock import local_today
from phd_searcher.config import Settings
from phd_searcher.config.llm import EmbeddingConfig
from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.engine.model_helper import ModelHelper, embedding_profile_for_model
from phd_searcher.engine.search_documents import (
    CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
    INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
    SEARCH_INDEX_CONTRACT_PAYLOAD,
)
from phd_searcher.opportunity_kinds import PROGRAMME, SPONTANEOUS, UNKNOWN, VACANCY
from phd_searcher.pipeline.family_feedback import FamilyFeedbackProfiles
from phd_searcher.pipeline.index import (
    _family_signal_for_position,
    _load_family_feedback_profiles,
    _verification_metadata,
)
from phd_searcher.pipeline.institutions import _build_entities

REPORT_SCHEMA_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = 4
READ_ONLY_SNAPSHOT_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)
SHADOW_BUILD_ID_PAYLOAD = "_phdbot_shadow_build_id"
SHADOW_CONTRACT_PAYLOAD = "_phdbot_shadow_contract"
_OLLAMA_DIGEST = re.compile(r"^(?:sha256:)?(?P<hex>[0-9a-f]{64})$", re.I)
_POSITION_KINDS = (UNKNOWN, VACANCY, PROGRAMME)
_POSITION_STATUSES = ("pending", "review", "eligible")
_INSTITUTION_KINDS = (VACANCY, PROGRAMME, SPONTANEOUS)

PositionRow = tuple[Position, University | None, ListingPage | None]


class _HashWriter(Protocol):
    def update(self, data: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    configured_model: str
    provider: str
    resolved_model: str
    digest: str | None
    profile: str
    manifest_dimension: int | None

    def report(self) -> dict[str, object]:
        return {
            "configured_model": self.configured_model,
            "provider": self.provider,
            "resolved_model": self.resolved_model,
            "digest": self.digest,
            "embedding_profile": self.profile,
            "manifest_dimension": self.manifest_dimension,
        }


@dataclass(frozen=True, slots=True)
class CollectionAudit:
    collection: str
    exists: bool
    exact_count: int
    dimension: int | None
    integer_ids: frozenset[int]
    non_integer_ids: tuple[str, ...]
    search_contracts: tuple[tuple[str | None, int], ...]
    build_ids: tuple[tuple[str | None, int], ...]
    shadow_contracts: tuple[tuple[str | None, int], ...]
    status: str | None = None
    optimizer_status: str | None = None
    optimizer_error: str | None = None
    distance: str | None = None

    def report(self) -> dict[str, object]:
        return {
            "collection": self.collection,
            "exists": self.exists,
            "exact_count": self.exact_count,
            "dimension": self.dimension,
            "distance": self.distance,
            "status": self.status,
            "optimizer_status": self.optimizer_status,
            "optimizer_error": self.optimizer_error,
            "integer_id_count": len(self.integer_ids),
            "integer_ids_sha256": id_set_sha256(self.integer_ids),
            "non_integer_ids": list(self.non_integer_ids),
            "search_index_contracts": _value_counts_report(self.search_contracts),
            "shadow_build_ids": _value_counts_report(self.build_ids),
            "shadow_document_contracts": _value_counts_report(
                self.shadow_contracts
            ),
        }


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    as_of: date
    position_ids: frozenset[int]
    institution_ids: frozenset[int]
    indexed_position_ids: frozenset[int]
    position_fingerprint: str
    institution_fingerprint: str


def _value_counts(values: Iterable[str | None]) -> tuple[tuple[str | None, int], ...]:
    counts = Counter(values)
    return tuple(sorted(counts.items(), key=lambda item: repr(item[0])))


def _value_counts_report(
    values: Sequence[tuple[str | None, int]],
) -> list[dict[str, object]]:
    return [{"value": value, "count": count} for value, count in values]


def id_set_sha256(ids: Iterable[int]) -> str:
    digest = hashlib.sha256()
    for value in sorted(ids):
        digest.update(str(value).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _hash_value(digest: _HashWriter, value: object) -> None:
    digest.update(repr(value).encode("utf-8", errors="surrogatepass"))
    digest.update(b"\0")


def position_snapshot_fingerprint(
    rows: Sequence[PositionRow],
    *,
    family_profiles: FamilyFeedbackProfiles,
) -> str:
    """Mirror the shadow builder's content fingerprint without importing it."""
    digest = hashlib.sha256()
    _hash_value(digest, CANDIDATE_SEARCH_DOCUMENT_CONTRACT)
    for position, university, listing_page in rows:
        for column in Position.__table__.columns:
            if column.name != "indexed_at":
                _hash_value(digest, getattr(position, column.name))
        if university is None:
            _hash_value(digest, None)
        else:
            for column in University.__table__.columns:
                _hash_value(digest, getattr(university, column.name))
        if listing_page is None:
            _hash_value(digest, None)
        else:
            for column in ListingPage.__table__.columns:
                _hash_value(digest, getattr(listing_page, column.name))
        _hash_value(
            digest,
            _family_signal_for_position(position, listing_page, family_profiles),
        )
    return digest.hexdigest()


def institution_snapshot_fingerprint(entities: Sequence[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    _hash_value(digest, INSTITUTION_SEARCH_DOCUMENT_CONTRACT)
    for entity in entities:
        _hash_value(
            digest,
            json.dumps(
                entity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )
    return digest.hexdigest()


def _position_candidate_statement(
    as_of: date,
) -> Select[tuple[Position, University, ListingPage]]:
    """Broad SQL prefilter; the shared current gate makes the final decision."""
    return (
        select(Position, University, ListingPage)
        .outerjoin(University, Position.university_id == University.id)
        .outerjoin(ListingPage, Position.listing_page_id == ListingPage.id)
        .where(Position.screening_status.in_(_POSITION_STATUSES))
        .where(Position.opportunity_kind.in_(_POSITION_KINDS))
        .where(Position.is_active.is_(True))
        .where(or_(Position.deadline.is_(None), Position.deadline >= as_of))
        .order_by(Position.id)
    )


def _institution_position_statement(
    as_of: date,
) -> Select[tuple[Position, University]]:
    return (
        select(Position, University)
        .outerjoin(University, Position.university_id == University.id)
        .where(Position.is_active.is_(True))
        .where(Position.screening_status == "eligible")
        .where(Position.opportunity_kind.in_(_INSTITUTION_KINDS))
        .where(or_(Position.deadline.is_(None), Position.deadline >= as_of))
        .order_by(Position.id)
    )


async def _materialize_database_snapshot(
    session: AsyncSession,
    *,
    as_of: date,
) -> DatabaseSnapshot:
    profiles = await _load_family_feedback_profiles(session)
    broad_rows = (await session.execute(_position_candidate_statement(as_of))).all()
    position_rows: list[PositionRow] = [
        (row[0], row[1], row[2])
        for row in broad_rows
        if _verification_metadata(row[0], as_of, listing_page=row[2]) is not None
    ]

    universities = list(
        (
            await session.execute(select(University).order_by(University.id))
        ).scalars().all()
    )
    institution_rows = (
        await session.execute(_institution_position_statement(as_of))
    ).all()
    entities = _build_entities(
        universities,
        [(row[0], row[1]) for row in institution_rows],
        today=as_of,
    )
    institution_ids = [entity.get("id") for entity in entities]
    typed_institution_ids = [
        entity_id for entity_id in institution_ids if isinstance(entity_id, int)
    ]
    if len(typed_institution_ids) != len(institution_ids):
        raise ValueError("institution snapshot contains a non-integer ID")
    if len(set(typed_institution_ids)) != len(typed_institution_ids):
        raise ValueError("institution snapshot contains duplicate IDs")
    indexed_ids = frozenset(
        (
            await session.execute(
                select(Position.id).where(Position.indexed_at.is_not(None))
            )
        ).scalars().all()
    )
    return DatabaseSnapshot(
        as_of=as_of,
        position_ids=frozenset(row[0].id for row in position_rows),
        institution_ids=frozenset(typed_institution_ids),
        indexed_position_ids=indexed_ids,
        position_fingerprint=position_snapshot_fingerprint(
            position_rows,
            family_profiles=profiles,
        ),
        institution_fingerprint=institution_snapshot_fingerprint(entities),
    )


async def load_database_snapshot(
    settings: Settings,
    *,
    as_of: date,
) -> DatabaseSnapshot:
    """Read all cutover inputs in one explicit immutable PostgreSQL snapshot."""
    engine = create_async_engine(
        settings.database.url,
        echo=False,
        future=True,
        poolclass=NullPool,
        execution_options={
            "schema_translate_map": {None: settings.database.schema_name}
        },
    )
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                # This is deliberately the first statement in the transaction.
                await connection.execute(text(READ_ONLY_SNAPSHOT_SQL))
                session = AsyncSession(
                    bind=connection,
                    expire_on_commit=False,
                    autoflush=False,
                )
                try:
                    return await _materialize_database_snapshot(
                        session,
                        as_of=as_of,
                    )
                finally:
                    await session.close()
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def _vector_dimension(info: CollectionInfo) -> int:
    vectors = _single_vector_params(info)
    dimension = getattr(vectors, "size", None)
    if not isinstance(dimension, int) or dimension <= 0:
        raise ValueError("cannot determine collection vector dimension")
    return dimension


def _single_vector_params(info: CollectionInfo) -> object:
    vectors = info.config.params.vectors
    if isinstance(vectors, dict):
        raise ValueError("collection must contain one unnamed vector")
    return vectors


def _enum_string(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else None


def _vector_distance(info: CollectionInfo) -> str | None:
    return _enum_string(getattr(_single_vector_params(info), "distance", None))


def _optimizer_health(info: CollectionInfo) -> tuple[str | None, str | None]:
    optimizer = getattr(info, "optimizer_status", None)
    status = _enum_string(optimizer)
    error = getattr(optimizer, "error", None)
    typed_error = error if isinstance(error, str) else None
    if status is None and typed_error is not None:
        status = "error"
    return status, typed_error


async def audit_collection(
    qdrant: AsyncQdrantClient,
    collection: str,
) -> CollectionAudit:
    """Scan every point and payload needed by the cutover contract."""
    if not await qdrant.collection_exists(collection):
        return CollectionAudit(
            collection=collection,
            exists=False,
            exact_count=0,
            dimension=None,
            integer_ids=frozenset(),
            non_integer_ids=(),
            search_contracts=(),
            build_ids=(),
            shadow_contracts=(),
        )
    info = await qdrant.get_collection(collection)
    optimizer_status, optimizer_error = _optimizer_health(info)
    exact_count = (await qdrant.count(collection, exact=True)).count
    integer_ids: set[int] = set()
    non_integer_ids: list[str] = []
    contracts: list[str | None] = []
    build_ids: list[str | None] = []
    shadow_contracts: list[str | None] = []
    offset: Any = None
    while True:
        points, offset = await qdrant.scroll(
            collection_name=collection,
            limit=512,
            offset=offset,
            with_payload=[
                SEARCH_INDEX_CONTRACT_PAYLOAD,
                SHADOW_BUILD_ID_PAYLOAD,
                SHADOW_CONTRACT_PAYLOAD,
            ],
            with_vectors=False,
        )
        for point in points:
            if isinstance(point.id, int):
                integer_ids.add(point.id)
            else:
                non_integer_ids.append(str(point.id))
            payload = point.payload or {}
            contracts.append(_optional_string(payload.get(SEARCH_INDEX_CONTRACT_PAYLOAD)))
            build_ids.append(_optional_string(payload.get(SHADOW_BUILD_ID_PAYLOAD)))
            shadow_contracts.append(
                _optional_string(payload.get(SHADOW_CONTRACT_PAYLOAD))
            )
        if offset is None:
            break
    return CollectionAudit(
        collection=collection,
        exists=True,
        exact_count=exact_count,
        dimension=_vector_dimension(info),
        integer_ids=frozenset(integer_ids),
        non_integer_ids=tuple(sorted(non_integer_ids)),
        search_contracts=_value_counts(contracts),
        build_ids=_value_counts(build_ids),
        shadow_contracts=_value_counts(shadow_contracts),
        status=_enum_string(info.status),
        optimizer_status=optimizer_status,
        optimizer_error=optimizer_error,
        distance=_vector_distance(info),
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _ollama_model_name(configured_model: str) -> str | None:
    if not configured_model.casefold().startswith("ollama/"):
        return None
    model = configured_model.split("/", 1)[1].strip()
    if not model:
        raise ValueError("Ollama embedding model cannot be empty")
    return model if ":" in model else f"{model}:latest"


def _ollama_base(api_base: str | None) -> str:
    base = (api_base or "http://localhost:11434").rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _embedding_dimensions(payload: object) -> set[int]:
    dimensions: set[int] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if (
                key.casefold().endswith("embedding_length")
                and isinstance(value, int)
                and value > 0
            ):
                dimensions.add(value)
            dimensions.update(_embedding_dimensions(value))
    elif isinstance(payload, list):
        for value in payload:
            dimensions.update(_embedding_dimensions(value))
    return dimensions


async def resolve_model_identity(
    config: EmbeddingConfig,
    *,
    client: httpx.AsyncClient,
) -> ModelIdentity:
    configured_model = config.model.strip()
    profile = embedding_profile_for_model(configured_model)
    ollama_model = _ollama_model_name(configured_model)
    if ollama_model is None:
        return ModelIdentity(
            configured_model=configured_model,
            provider=configured_model.partition("/")[0] or "unknown",
            resolved_model=configured_model,
            digest=None,
            profile=profile,
            manifest_dimension=None,
        )

    base = _ollama_base(config.api_base)
    tags_response = await client.get(f"{base}/api/tags")
    tags_response.raise_for_status()
    models = tags_response.json().get("models")
    if not isinstance(models, list):
        raise ValueError("Ollama /api/tags returned an invalid models list")
    match: dict[str, object] | None = None
    for candidate in models:
        if isinstance(candidate, dict) and ollama_model in {
            candidate.get("name"),
            candidate.get("model"),
        }:
            match = candidate
            break
    if match is None:
        raise ValueError(f"Ollama embedding model {ollama_model!r} is not installed")
    raw_digest = match.get("digest")
    digest_match = (
        _OLLAMA_DIGEST.fullmatch(raw_digest)
        if isinstance(raw_digest, str)
        else None
    )
    if digest_match is None:
        raise ValueError(f"Ollama did not expose a sha256 digest for {ollama_model!r}")

    show_response = await client.post(
        f"{base}/api/show",
        json={"model": ollama_model},
    )
    show_response.raise_for_status()
    dimensions = _embedding_dimensions(show_response.json().get("model_info"))
    if len(dimensions) > 1:
        raise ValueError(
            f"Ollama manifest exposes ambiguous embedding dimensions: {sorted(dimensions)}"
        )
    return ModelIdentity(
        configured_model=configured_model,
        provider="ollama",
        resolved_model=ollama_model,
        digest=f"sha256:{digest_match.group('hex').lower()}",
        profile=profile,
        manifest_dimension=next(iter(dimensions), None),
    )


def checkpoint_path(collection: str, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    safe_name = "".join(
        char if char.isalnum() or char in "-_." else "_" for char in collection
    )
    return Path("var") / "shadow-index" / f"{safe_name}.json"


def read_checkpoint(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, f"checkpoint does not exist: {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, f"cannot read checkpoint {path}: {type(error).__name__}: {error}"
    if not isinstance(payload, dict):
        return None, f"checkpoint is not a JSON object: {path}"
    return payload, None


def _single_value_count(
    values: Sequence[tuple[str | None, int]],
    *,
    expected_value: str,
    expected_count: int,
) -> bool:
    return tuple(values) == ((expected_value, expected_count),)


def _blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _checkpoint_summary(
    path: Path,
    checkpoint: dict[str, Any] | None,
    error: str | None,
) -> dict[str, object]:
    if checkpoint is None:
        return {"path": str(path), "exists": path.exists(), "error": error}
    return {
        "path": str(path),
        "exists": True,
        "schema_version": checkpoint.get("schema_version"),
        "status": checkpoint.get("status"),
        "as_of": checkpoint.get("as_of"),
        "build_id": checkpoint.get("build_id"),
        "collection": checkpoint.get("collection"),
        "institution_collection": checkpoint.get("institution_collection"),
        "document_contract": checkpoint.get("document_contract"),
        "document_contracts": checkpoint.get("document_contracts"),
        "embedding_profile": checkpoint.get("embedding_profile"),
        "embedding": checkpoint.get("embedding"),
        "embedding_dimension": checkpoint.get("embedding_dimension"),
        "positions": checkpoint.get("positions"),
        "institutions": checkpoint.get("institutions"),
    }


def build_cutover_report(
    *,
    production_collection: str,
    collection: str,
    institution_collection: str,
    model_identity: ModelIdentity,
    position_contract: str,
    institution_contract: str,
    database: DatabaseSnapshot,
    positions: CollectionAudit,
    institutions: CollectionAudit,
    checkpoint_file: Path,
    checkpoint: dict[str, Any] | None,
    checkpoint_error: str | None,
) -> dict[str, object]:
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if collection == institution_collection:
        blockers.append(_blocker("duplicate_collections", "position and institution collections must differ"))
    protected = {production_collection, f"{production_collection}_institutions"}
    if collection in protected or institution_collection in protected:
        blockers.append(_blocker("production_target", "a proposed collection is already configured as production"))

    if checkpoint is None:
        blockers.append(_blocker("checkpoint_unavailable", checkpoint_error or "checkpoint unavailable"))
    else:
        if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            blockers.append(_blocker("checkpoint_schema_version_mismatch", f"checkpoint schema version is not {CHECKPOINT_SCHEMA_VERSION}"))
        if checkpoint.get("status") != "complete":
            blockers.append(_blocker("checkpoint_incomplete", "shadow checkpoint status is not complete"))
        if checkpoint.get("collection") != collection or checkpoint.get("institution_collection") != institution_collection:
            blockers.append(_blocker("checkpoint_targets_mismatch", "checkpoint collection names do not match the requested targets"))
        embedding = checkpoint.get("embedding")
        expected_embedding = {
            "configured_model": model_identity.configured_model,
            "provider": model_identity.provider,
            "resolved_model": model_identity.resolved_model,
            "digest": model_identity.digest,
        }
        if embedding != expected_embedding:
            blockers.append(_blocker("checkpoint_model_mismatch", "checkpoint embedding identity does not match the exact requested model"))
        if checkpoint.get("embedding_profile") != model_identity.profile:
            blockers.append(_blocker("checkpoint_profile_mismatch", "checkpoint embedding input profile differs from the requested model"))

        document_contracts = checkpoint.get("document_contracts")
        if document_contracts != {
            "positions": CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
            "institutions": INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
        }:
            blockers.append(_blocker("checkpoint_document_contract_mismatch", "checkpoint does not pin both current document contracts"))
        for section_name, count, fingerprint in (
            ("positions", len(database.position_ids), database.position_fingerprint),
            ("institutions", len(database.institution_ids), database.institution_fingerprint),
        ):
            section = checkpoint.get(section_name)
            if not isinstance(section, dict) or section.get("status") != "complete":
                blockers.append(_blocker(f"checkpoint_{section_name}_incomplete", f"checkpoint {section_name} section is not complete"))
                continue
            if section.get("expected_count") != count or section.get("completed") != count:
                blockers.append(_blocker(f"checkpoint_{section_name}_count_mismatch", f"checkpoint {section_name} counts differ from the current snapshot"))
            if section.get("fingerprint") != fingerprint:
                blockers.append(_blocker(f"checkpoint_{section_name}_snapshot_mismatch", f"checkpoint {section_name} fingerprint differs from the current snapshot"))
        checkpoint_build_id = checkpoint.get("build_id")
        if not isinstance(checkpoint_build_id, str) or not checkpoint_build_id.strip():
            blockers.append(_blocker("checkpoint_build_id_invalid", "checkpoint does not contain a nonempty build ID"))
        if checkpoint.get("as_of") != database.as_of.isoformat():
            blockers.append(_blocker("checkpoint_as_of_mismatch", "checkpoint date differs from the current gate date; derived verification and uncertainty payloads may have aged"))

    checkpoint_dimension = checkpoint.get("embedding_dimension") if checkpoint else None
    expected_dimension = model_identity.manifest_dimension
    if expected_dimension is None and isinstance(checkpoint_dimension, int) and checkpoint_dimension > 0:
        expected_dimension = checkpoint_dimension
        warnings.append(_warning("dimension_from_checkpoint", "model provider did not expose a dimension; using the pinned checkpoint dimension"))
    elif expected_dimension is None:
        blockers.append(_blocker("model_dimension_unknown", "neither the model manifest nor checkpoint exposes an embedding dimension"))
    if (
        model_identity.manifest_dimension is not None
        and checkpoint_dimension != model_identity.manifest_dimension
    ):
        blockers.append(_blocker("checkpoint_dimension_mismatch", "checkpoint dimension differs from the exact model manifest"))

    collection_specs = (
        ("positions", positions, database.position_ids, position_contract, CANDIDATE_SEARCH_DOCUMENT_CONTRACT),
        ("institutions", institutions, database.institution_ids, institution_contract, INSTITUTION_SEARCH_DOCUMENT_CONTRACT),
    )
    checkpoint_build_id = checkpoint.get("build_id") if checkpoint else None
    for label, audit, expected_ids, expected_contract, document_contract in collection_specs:
        if not audit.exists:
            blockers.append(_blocker(f"{label}_collection_missing", f"{audit.collection!r} does not exist"))
            continue
        if audit.non_integer_ids or audit.exact_count != len(audit.integer_ids):
            blockers.append(_blocker(f"{label}_invalid_ids", f"{audit.collection!r} contains non-integer, duplicate, or unscanned IDs"))
        if audit.status != "green":
            blockers.append(_blocker(f"{label}_collection_not_green", f"{audit.collection!r} status is {audit.status!r}, not 'green'"))
        if audit.optimizer_status is not None and audit.optimizer_status != "ok":
            detail = f": {audit.optimizer_error}" if audit.optimizer_error else ""
            blockers.append(_blocker(f"{label}_optimizer_not_ok", f"{audit.collection!r} optimizer status is {audit.optimizer_status!r}, not 'ok'{detail}"))
        if audit.integer_ids != expected_ids:
            blockers.append(_blocker(f"{label}_membership_mismatch", f"{audit.collection!r} does not exactly match the current safe IDs"))
        if audit.dimension != expected_dimension:
            blockers.append(_blocker(f"{label}_dimension_mismatch", f"{audit.collection!r} dimension does not match the exact embedding model"))
        if audit.distance != Distance.COSINE.value:
            blockers.append(_blocker(f"{label}_distance_mismatch", f"{audit.collection!r} distance is {audit.distance!r}, not {Distance.COSINE.value!r}"))
        if not _single_value_count(audit.search_contracts, expected_value=expected_contract, expected_count=audit.exact_count):
            blockers.append(_blocker(f"{label}_search_contract_mismatch", f"not every point in {audit.collection!r} has the expected search-index contract"))
        if not isinstance(checkpoint_build_id, str) or not _single_value_count(
            audit.build_ids,
            expected_value=checkpoint_build_id if isinstance(checkpoint_build_id, str) else "",
            expected_count=audit.exact_count,
        ):
            blockers.append(_blocker(f"{label}_ownership_mismatch", f"not every point in {audit.collection!r} belongs to the checkpoint build"))
        if not _single_value_count(audit.shadow_contracts, expected_value=document_contract, expected_count=audit.exact_count):
            blockers.append(_blocker(f"{label}_shadow_contract_mismatch", f"not every point in {audit.collection!r} has the expected shadow document contract"))

    db_mark_indexed = database.position_ids - database.indexed_position_ids
    db_clear_indexed = database.indexed_position_ids - database.position_ids
    if db_mark_indexed or db_clear_indexed:
        warnings.append(_warning("db_membership_alignment_required", "a later atomic cutover must align positions.indexed_at with the validated shadow membership"))

    position_missing = database.position_ids - positions.integer_ids
    position_unexpected = positions.integer_ids - database.position_ids
    institution_missing = database.institution_ids - institutions.integer_ids
    institution_unexpected = institutions.integer_ids - database.institution_ids
    ready = not blockers
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "ready_for_atomic_cutover": ready,
        "blockers": blockers,
        "warnings": warnings,
        "requested": {
            "production_collection": production_collection,
            "position_collection": collection,
            "institution_collection": institution_collection,
            "embedding_model": model_identity.configured_model,
        },
        "model": {
            **model_identity.report(),
            "expected_dimension": expected_dimension,
            "position_search_index_contract": position_contract,
            "institution_search_index_contract": institution_contract,
        },
        "checkpoint": _checkpoint_summary(checkpoint_file, checkpoint, checkpoint_error),
        "database_snapshot": {
            "isolation": "REPEATABLE READ READ ONLY",
            "as_of": database.as_of.isoformat(),
            "safe_position_count": len(database.position_ids),
            "safe_position_ids_sha256": id_set_sha256(database.position_ids),
            "position_fingerprint": database.position_fingerprint,
            "institution_count": len(database.institution_ids),
            "institution_ids_sha256": id_set_sha256(database.institution_ids),
            "institution_fingerprint": database.institution_fingerprint,
            "indexed_at_membership_count": len(database.indexed_position_ids),
            "indexed_at_membership_sha256": id_set_sha256(database.indexed_position_ids),
        },
        "collections": {
            "positions": positions.report(),
            "institutions": institutions.report(),
        },
        "reconciliation": {
            "positions": {
                "safe_missing_from_shadow_ids": sorted(position_missing),
                "shadow_not_safe_ids": sorted(position_unexpected),
                "db_indexed_at_to_set_ids": sorted(db_mark_indexed),
                "db_indexed_at_to_clear_ids": sorted(db_clear_indexed),
            },
            "institutions": {
                "safe_missing_from_shadow_ids": sorted(institution_missing),
                "shadow_not_safe_ids": sorted(institution_unexpected),
            },
        },
        "cutover_manifest": {
            "position_collection": collection,
            "institution_collection": institution_collection,
            "embedding_model": model_identity.configured_model,
            "embedding_digest": model_identity.digest,
            "embedding_dimension": expected_dimension,
            "position_search_index_contract": position_contract,
            "institution_search_index_contract": institution_contract,
            "safe_position_ids": sorted(database.position_ids),
            "safe_institution_ids": sorted(database.institution_ids),
            "db_indexed_at_to_set_ids": sorted(db_mark_indexed),
            "db_indexed_at_to_clear_ids": sorted(db_clear_indexed),
            "backup_required_before_mutation": True,
        },
    }


async def plan_cutover(args: argparse.Namespace, settings: Settings) -> dict[str, object]:
    embedding = settings.embedding.model_copy(update={"model": args.embedding_model})
    model = ModelHelper(settings.llm, embedding)
    checkpoint_file = checkpoint_path(args.collection, args.checkpoint)
    checkpoint, checkpoint_error = read_checkpoint(checkpoint_file)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        identity = await resolve_model_identity(embedding, client=client)

    database = await load_database_snapshot(settings, as_of=local_today())
    qdrant = AsyncQdrantClient(url=settings.qdrant.url)
    try:
        positions, institutions = await asyncio.gather(
            audit_collection(qdrant, args.collection),
            audit_collection(qdrant, args.institution_collection),
        )
    finally:
        await qdrant.close()
    return build_cutover_report(
        production_collection=settings.qdrant.collection,
        collection=args.collection,
        institution_collection=args.institution_collection,
        model_identity=identity,
        position_contract=model.search_index_contract(),
        institution_contract=model.search_index_contract(institutions=True),
        database=database,
        positions=positions,
        institutions=institutions,
        checkpoint_file=checkpoint_file,
        checkpoint=checkpoint,
        checkpoint_error=checkpoint_error,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only exact preflight for a PHDBOT search shadow cutover."
    )
    parser.add_argument("--collection", required=True, help="position shadow collection")
    parser.add_argument(
        "--institution-collection",
        required=True,
        help="institution/group shadow collection",
    )
    parser.add_argument(
        "--embedding-model",
        required=True,
        help="exact LiteLLM embedding model used to build both shadows",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help="shadow-builder checkpoint (default: derived from --collection)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write the JSON manifest here in addition to the exit status",
    )
    return parser


async def _main() -> int:
    load_dotenv()
    args = build_parser().parse_args()
    try:
        report = await plan_cutover(args, Settings())
    except Exception as error:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "read_only": True,
            "ready_for_atomic_cutover": False,
            "fatal_error": {
                "type": type(error).__name__,
                "message": str(error),
            },
        }
        payload = json.dumps(report, ensure_ascii=False, sort_keys=True)
        if args.output:
            args.output.write_text(payload + "\n", encoding="utf-8")
        else:
            print(payload)
        return 1
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0 if report["ready_for_atomic_cutover"] else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
