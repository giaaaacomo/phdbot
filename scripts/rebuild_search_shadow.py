"""Build and validate a disposable search index without touching production state.

The command deliberately separates PostgreSQL authority from an experimental
Qdrant collection:

* PostgreSQL is opened in an explicit read-only transaction;
* ``positions.indexed_at`` and screening verdicts are never changed;
* both destination collection names must be explicit and must not already
  exist (except when resuming this command's own checkpoint);
* the production position and institution collections are always refused;
* a content fingerprint prevents a resumed build from mixing two DB snapshots.

Example::

    uv run python scripts/rebuild_search_shadow.py \
        --collection positions_nomic_compact_v1 \
        --institution-collection positions_nomic_compact_v1_institutions

To evaluate a model other than the one configured in ``.env``, pass its exact
LiteLLM model string with ``--embedding-model``.  The override is local to this
process; the configured API base and key are retained and no settings are
mutated.

If the process is interrupted, pass the same arguments plus ``--resume``.
The completed collection can then be queried directly for evaluation.  This
script never swaps aliases or changes the configured production collection.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import CollectionInfo, Distance, PointStruct, VectorParams
from sqlalchemy import and_, or_, select, text
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
    build_institution_search_document,
)
from phd_searcher.opportunity_kinds import PROGRAMME, SPONTANEOUS, UNKNOWN, VACANCY
from phd_searcher.pipeline.family_feedback import FamilyFeedbackProfiles
from phd_searcher.pipeline.index import (
    _embed_and_upsert,
    _family_signal_for_position,
    _load_family_feedback_profiles,
    _verification_metadata,
)
from phd_searcher.pipeline.institutions import _build_entities

CHECKPOINT_SCHEMA_VERSION = 4
LEGACY_CHECKPOINT_SCHEMA_VERSION = 3
POSITION_DOCUMENT_CONTRACT = CANDIDATE_SEARCH_DOCUMENT_CONTRACT
INSTITUTION_DOCUMENT_CONTRACT = INSTITUTION_SEARCH_DOCUMENT_CONTRACT
# Kept as a compatibility alias for callers that imported the old, ambiguous
# name. New checkpoint and ownership code must use the scoped constants above.
DOCUMENT_CONTRACT = POSITION_DOCUMENT_CONTRACT
DIMENSION_PROBE_DOCUMENT = "PHDBOT shadow index embedding dimension probe"
DEFAULT_BATCH_SIZE = 64
SHADOW_BUILD_ID_PAYLOAD = "_phdbot_shadow_build_id"
SHADOW_CONTRACT_PAYLOAD = "_phdbot_shadow_contract"
READ_ONLY_SNAPSHOT_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)
_OLLAMA_DIGEST = re.compile(r"^(?:sha256:)?(?P<hex>[0-9a-f]{64})$", re.I)
_POSITION_INDEX_KINDS = (VACANCY, PROGRAMME)
_PROVISIONAL_INDEX_KINDS = (UNKNOWN, VACANCY, PROGRAMME)
_PROVISIONAL_SCREENING_STATUSES = ("pending", "review", "eligible")
_INSTITUTION_SOURCE_KINDS = (VACANCY, PROGRAMME, SPONTANEOUS)

PositionRow = tuple[Position, University | None, ListingPage | None]


class _HashWriter(Protocol):
    def update(self, data: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class CollectionValidation:
    collection: str
    expected_count: int
    actual_count: int
    dimension: int
    missing_ids: tuple[int, ...]
    unexpected_ids: tuple[int, ...]
    ownership_mismatch_ids: tuple[int, ...] = ()
    search_contract_mismatch_ids: tuple[int, ...] = ()

    @property
    def valid(self) -> bool:
        return (
            self.expected_count == self.actual_count
            and not self.missing_ids
            and not self.unexpected_ids
            and not self.ownership_mismatch_ids
            and not self.search_contract_mismatch_ids
            and self.dimension > 0
        )


@dataclass(frozen=True, slots=True)
class CollectionPointAudit:
    ids: frozenset[int]
    ownership_mismatch_ids: tuple[int, ...]
    search_contract_mismatch_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class EmbeddingIdentity:
    configured_model: str
    provider: str
    resolved_model: str
    digest: str | None
    profile: str

    def checkpoint_payload(self) -> dict[str, str | None]:
        return {
            "configured_model": self.configured_model,
            "provider": self.provider,
            "resolved_model": self.resolved_model,
            "digest": self.digest,
        }


def _chunks[T](values: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def validate_target_names(
    *,
    production_collection: str,
    collection: str,
    institution_collection: str | None,
) -> None:
    """Refuse implicit, duplicate, and production destinations."""
    requested = [collection]
    if institution_collection:
        requested.append(institution_collection)
    if any(not name.strip() for name in requested):
        raise ValueError("shadow collection names cannot be empty")
    if len(set(requested)) != len(requested):
        raise ValueError("position and institution shadow collections must differ")
    protected = {
        production_collection,
        f"{production_collection}_institutions",
    }
    overlap = protected.intersection(requested)
    if overlap:
        raise ValueError(
            "refusing to write production collection(s): "
            + ", ".join(sorted(overlap))
        )


def _ollama_model_name(configured_model: str) -> str | None:
    prefix = "ollama/"
    if not configured_model.casefold().startswith(prefix):
        return None
    requested = configured_model[len(prefix) :].strip()
    if not requested:
        raise ValueError("Ollama embedding model name cannot be empty")
    return requested if ":" in requested else f"{requested}:latest"


def _ollama_native_base(api_base: str | None) -> str:
    base = (api_base or "http://localhost:11434").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def embedding_config_for_run(
    configured: EmbeddingConfig,
    model_override: str | None,
) -> EmbeddingConfig:
    """Return an isolated per-run config, retaining provider connection data."""
    if model_override is None:
        return configured
    model = model_override.strip()
    if not model:
        raise ValueError("embedding model override cannot be empty")
    return configured.model_copy(update={"model": model})


async def resolve_embedding_identity(
    config: EmbeddingConfig,
    *,
    client: httpx.AsyncClient,
) -> EmbeddingIdentity:
    """Pin Ollama tags to their immutable local manifest digest."""
    profile = embedding_profile_for_model(config.model)
    ollama_model = _ollama_model_name(config.model)
    if ollama_model is None:
        return EmbeddingIdentity(
            configured_model=config.model,
            provider=config.model.partition("/")[0] or "unknown",
            resolved_model=config.model,
            digest=None,
            profile=profile,
        )

    response = await client.get(f"{_ollama_native_base(config.api_base)}/api/tags")
    response.raise_for_status()
    payload = response.json()
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ValueError("Ollama /api/tags returned an invalid models list")
    matched: dict[str, object] | None = None
    for candidate in models:
        if not isinstance(candidate, dict):
            continue
        names = {candidate.get("name"), candidate.get("model")}
        if ollama_model in names:
            matched = candidate
            break
    if matched is None:
        raise ValueError(
            f"configured Ollama embedding model {ollama_model!r} is not installed"
        )
    digest = matched.get("digest")
    digest_match = _OLLAMA_DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    if digest_match is None:
        raise ValueError(
            f"Ollama did not expose a sha256 digest for {ollama_model!r}"
        )
    canonical_digest = f"sha256:{digest_match.group('hex').lower()}"
    return EmbeddingIdentity(
        configured_model=config.model,
        provider="ollama",
        resolved_model=ollama_model,
        digest=canonical_digest,
        profile=profile,
    )


def _position_candidate_statement(
    as_of: date,
) -> Select[tuple[Position, University, ListingPage]]:
    """Return the same broad candidates as index.run, without indexed_at."""
    verified = and_(
        Position.screening_status == "eligible",
        Position.opportunity_kind.in_(_POSITION_INDEX_KINDS),
    )
    provisional = and_(
        Position.screening_status.in_(_PROVISIONAL_SCREENING_STATUSES),
        Position.opportunity_kind.in_(_PROVISIONAL_INDEX_KINDS),
    )
    return (
        select(Position, University, ListingPage)
        .outerjoin(University, Position.university_id == University.id)
        .outerjoin(ListingPage, Position.listing_page_id == ListingPage.id)
        .where(or_(verified, provisional))
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
        .where(Position.opportunity_kind.in_(_INSTITUTION_SOURCE_KINDS))
        .where(or_(Position.deadline.is_(None), Position.deadline >= as_of))
        .order_by(Position.id)
    )


def _hash_value(digest: _HashWriter, value: object) -> None:
    digest.update(repr(value).encode("utf-8", errors="surrogatepass"))
    digest.update(b"\0")


def position_snapshot_fingerprint(
    rows: Sequence[PositionRow],
    *,
    family_profiles: FamilyFeedbackProfiles,
) -> str:
    """Hash every field that can influence eligibility, vector text, or payload."""
    digest = hashlib.sha256()
    _hash_value(digest, POSITION_DOCUMENT_CONTRACT)
    for position, university, listing_page in rows:
        for column in Position.__table__.columns:
            if column.name != "indexed_at":
                _hash_value(digest, getattr(position, column.name))
        if university is not None:
            for column in University.__table__.columns:
                _hash_value(digest, getattr(university, column.name))
        else:
            _hash_value(digest, None)
        if listing_page is not None:
            for column in ListingPage.__table__.columns:
                _hash_value(digest, getattr(listing_page, column.name))
        else:
            _hash_value(digest, None)
        _hash_value(
            digest,
            _family_signal_for_position(position, listing_page, family_profiles),
        )
    return digest.hexdigest()


def institution_snapshot_fingerprint(entities: Sequence[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    _hash_value(digest, INSTITUTION_DOCUMENT_CONTRACT)
    for entity in entities:
        encoded = json.dumps(
            entity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        _hash_value(digest, encoded)
    return digest.hexdigest()


def _checkpoint_path(collection: str, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    safe_name = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in collection
    )
    return Path("var") / "shadow-index" / f"{safe_name}.json"


def read_checkpoint(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid checkpoint object in {path}")
    return payload


def write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def validate_checkpoint_contract(
    checkpoint: dict[str, Any],
    *,
    collection: str,
    institution_collection: str | None,
    embedding_identity: EmbeddingIdentity,
    embedding_dimension: int,
) -> date:
    expected = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "document_contracts": {
            "positions": POSITION_DOCUMENT_CONTRACT,
            "institutions": (
                INSTITUTION_DOCUMENT_CONTRACT
                if institution_collection is not None
                else None
            ),
        },
        "embedding_profile": embedding_identity.profile,
        "embedding": embedding_identity.checkpoint_payload(),
        "embedding_dimension": embedding_dimension,
        "collection": collection,
        "institution_collection": institution_collection,
    }
    mismatches = {
        key: (checkpoint.get(key), value)
        for key, value in expected.items()
        if checkpoint.get(key) != value
    }
    if mismatches:
        details = ", ".join(
            f"{key}={actual!r} (expected {wanted!r})"
            for key, (actual, wanted) in mismatches.items()
        )
        raise ValueError(f"checkpoint contract mismatch: {details}")
    build_id = checkpoint.get("build_id")
    if not isinstance(build_id, str) or not build_id.strip():
        raise ValueError("checkpoint is missing a valid build_id")
    raw_as_of = checkpoint.get("as_of")
    if not isinstance(raw_as_of, str):
        raise ValueError("checkpoint is missing as_of")
    return date.fromisoformat(raw_as_of)


async def _collection_ids(
    qdrant: AsyncQdrantClient,
    collection: str,
) -> set[int]:
    if not await qdrant.collection_exists(collection):
        return set()
    ids: set[int] = set()
    offset: Any = None
    while True:
        points, offset = await qdrant.scroll(
            collection_name=collection,
            limit=512,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.update(int(point.id) for point in points if isinstance(point.id, int))
        if offset is None:
            break
    return ids


async def _audit_collection_points(
    qdrant: AsyncQdrantClient,
    *,
    collection: str,
    build_id: str,
    document_contract: str,
    search_index_contract: str,
    allowed_unmarked_ids: set[int] | None = None,
    allowed_document_contracts: frozenset[str] | None = None,
) -> CollectionPointAudit:
    """Audit ownership and semantic-vector contracts for every collection point.

    ``allowed_unmarked_ids`` covers the narrow crash window after a batch
    upsert and before its ownership payload is attached. Those IDs are not
    returned, so the caller safely overwrites the incomplete batch.
    """
    if not await qdrant.collection_exists(collection):
        return CollectionPointAudit(frozenset(), (), ())
    allowed_unmarked_ids = allowed_unmarked_ids or set()
    allowed_contracts = allowed_document_contracts or frozenset(
        {document_contract}
    )
    ids: set[int] = set()
    ownership_mismatches: list[int] = []
    search_contract_mismatches: list[int] = []
    offset: Any = None
    while True:
        points, offset = await qdrant.scroll(
            collection_name=collection,
            limit=512,
            offset=offset,
            with_payload=[
                SHADOW_BUILD_ID_PAYLOAD,
                SHADOW_CONTRACT_PAYLOAD,
                SEARCH_INDEX_CONTRACT_PAYLOAD,
            ],
            with_vectors=False,
        )
        for point in points:
            if not isinstance(point.id, int):
                raise ValueError("shadow collection contains a non-integer point ID")
            payload = point.payload or {}
            owner = payload.get(SHADOW_BUILD_ID_PAYLOAD)
            contract = payload.get(SHADOW_CONTRACT_PAYLOAD)
            if point.id in allowed_unmarked_ids and owner is None and contract is None:
                continue
            ids.add(point.id)
            if owner != build_id or contract not in allowed_contracts:
                ownership_mismatches.append(point.id)
            if payload.get(SEARCH_INDEX_CONTRACT_PAYLOAD) != search_index_contract:
                search_contract_mismatches.append(point.id)
        if offset is None:
            break
    return CollectionPointAudit(
        ids=frozenset(ids),
        ownership_mismatch_ids=tuple(sorted(ownership_mismatches)),
        search_contract_mismatch_ids=tuple(sorted(search_contract_mismatches)),
    )


async def _trusted_collection_ids(
    qdrant: AsyncQdrantClient,
    *,
    collection: str,
    build_id: str,
    document_contract: str,
    search_index_contract: str,
    allowed_unmarked_ids: set[int] | None = None,
) -> set[int]:
    """Return only points proven to belong to the exact embedding contract."""
    audit = await _audit_collection_points(
        qdrant,
        collection=collection,
        build_id=build_id,
        document_contract=document_contract,
        search_index_contract=search_index_contract,
        allowed_unmarked_ids=allowed_unmarked_ids,
    )
    if audit.ownership_mismatch_ids:
        raise ValueError(
            f"collection {collection!r} is not owned by shadow build {build_id} "
            f"under document contract {document_contract!r}; refusing to modify it "
            f"(first mismatched IDs: {audit.ownership_mismatch_ids[:10]})"
        )
    if audit.search_contract_mismatch_ids:
        raise ValueError(
            f"collection {collection!r} contains vectors with an unknown or "
            f"different search contract; refusing to relabel or modify them "
            f"(expected {search_index_contract!r}; first mismatched IDs: "
            f"{audit.search_contract_mismatch_ids[:10]})"
        )
    return set(audit.ids)


def _vector_dimension(collection_info: CollectionInfo) -> int:
    vectors = collection_info.config.params.vectors
    if isinstance(vectors, dict):
        if len(vectors) != 1:
            raise ValueError("shadow collection must contain one unnamed vector")
        vectors = next(iter(vectors.values()))
    size = getattr(vectors, "size", None)
    if not isinstance(size, int) or size <= 0:
        raise ValueError("cannot determine shadow collection vector dimension")
    return size


async def resolve_embedding_dimension(model: ModelHelper) -> int:
    vectors = await model.embed_documents([DIMENSION_PROBE_DOCUMENT])
    if len(vectors) != 1 or not vectors[0]:
        raise ValueError("embedding model returned an invalid dimension probe")
    return len(vectors[0])


async def validate_existing_collection_dimensions(
    qdrant: AsyncQdrantClient,
    *,
    collections: Sequence[str],
    expected_dimension: int,
) -> None:
    """Validate every existing resume target before the first upsert."""
    for collection in collections:
        if not await qdrant.collection_exists(collection):
            continue
        actual_dimension = _vector_dimension(await qdrant.get_collection(collection))
        if actual_dimension != expected_dimension:
            raise ValueError(
                f"shadow collection {collection!r} has dimension {actual_dimension}; "
                f"pinned embedding expects {expected_dimension}. No points were written."
            )


def _validate_legacy_checkpoint_contract(
    checkpoint: dict[str, Any],
    *,
    collection: str,
    institution_collection: str | None,
    embedding_identity: EmbeddingIdentity,
    embedding_dimension: int,
) -> tuple[date, str]:
    """Validate the non-ambiguous parts of a completed schema-v3 checkpoint."""
    expected = {
        "schema_version": LEGACY_CHECKPOINT_SCHEMA_VERSION,
        "embedding_profile": embedding_identity.profile,
        "embedding": embedding_identity.checkpoint_payload(),
        "embedding_dimension": embedding_dimension,
        "collection": collection,
        "institution_collection": institution_collection,
        "status": "complete",
    }
    mismatches = {
        key: (checkpoint.get(key), value)
        for key, value in expected.items()
        if checkpoint.get(key) != value
    }
    if mismatches:
        details = ", ".join(
            f"{key}={actual!r} (expected {wanted!r})"
            for key, (actual, wanted) in mismatches.items()
        )
        raise ValueError(
            "schema-v3 checkpoints can only be migrated after a complete build: "
            + details
        )
    legacy_document_contract = checkpoint.get("document_contract")
    if not isinstance(legacy_document_contract, str) or not legacy_document_contract:
        raise ValueError(
            "schema-v3 checkpoint has no document contract; use fresh shadow names"
        )
    build_id = checkpoint.get("build_id")
    if not isinstance(build_id, str) or not build_id.strip():
        raise ValueError("schema-v3 checkpoint is missing a valid build_id")
    raw_as_of = checkpoint.get("as_of")
    if not isinstance(raw_as_of, str):
        raise ValueError("schema-v3 checkpoint is missing as_of")
    return date.fromisoformat(raw_as_of), legacy_document_contract


async def migrate_completed_v3_checkpoint(
    checkpoint: dict[str, Any],
    *,
    qdrant: AsyncQdrantClient,
    model: ModelHelper,
    collection: str,
    institution_collection: str | None,
    embedding_identity: EmbeddingIdentity,
    embedding_dimension: int,
) -> date:
    """Safely split an old ambiguous document contract into scoped contracts.

    Schema v3 marked both collections with the candidate document contract.
    Relabelling is permitted only for a completed build and only after every
    point in every destination proves the exact current search-index contract.
    No payload is changed if any collection, point, count, or dimension fails
    that proof. The operation is idempotent if interrupted after relabelling.
    """
    as_of, legacy_document_contract = _validate_legacy_checkpoint_contract(
        checkpoint,
        collection=collection,
        institution_collection=institution_collection,
        embedding_identity=embedding_identity,
        embedding_dimension=embedding_dimension,
    )
    build_id = str(checkpoint["build_id"])
    plans = [
        (
            "positions",
            collection,
            POSITION_DOCUMENT_CONTRACT,
            model.search_index_contract(),
        )
    ]
    if institution_collection is not None:
        plans.append(
            (
                "institutions",
                institution_collection,
                INSTITUTION_DOCUMENT_CONTRACT,
                model.search_index_contract(institutions=True),
            )
        )

    validated_ids: dict[str, tuple[str, set[int], str, str]] = {}
    for section, target, document_contract, search_contract in plans:
        section_payload = checkpoint.get(section)
        if (
            not isinstance(section_payload, dict)
            or section_payload.get("status") != "complete"
            or section_payload.get("inflight_ids")
        ):
            raise ValueError(
                f"schema-v3 {section} section is not complete; use fresh shadow names"
            )
        expected_count = section_payload.get("expected_count")
        if not isinstance(expected_count, int) or expected_count < 0:
            raise ValueError(
                f"schema-v3 {section} section has no valid expected_count"
            )
        if not await qdrant.collection_exists(target):
            raise ValueError(
                f"schema-v3 collection {target!r} is missing; use fresh shadow names"
            )
        actual_dimension = _vector_dimension(await qdrant.get_collection(target))
        if actual_dimension != embedding_dimension:
            raise ValueError(
                f"schema-v3 collection {target!r} has dimension {actual_dimension}; "
                f"expected {embedding_dimension}. No payloads were changed."
            )
        audit = await _audit_collection_points(
            qdrant,
            collection=target,
            build_id=build_id,
            document_contract=document_contract,
            search_index_contract=search_contract,
            allowed_document_contracts=frozenset(
                {legacy_document_contract, document_contract}
            ),
        )
        actual_count = (await qdrant.count(target, exact=True)).count
        if actual_count != expected_count or len(audit.ids) != expected_count:
            raise ValueError(
                f"schema-v3 collection {target!r} count is {actual_count}; "
                f"expected {expected_count}. No payloads were changed."
            )
        if audit.ownership_mismatch_ids or audit.search_contract_mismatch_ids:
            raise ValueError(
                f"schema-v3 collection {target!r} cannot be migrated: every point "
                "must have the original build owner and the exact current search "
                f"contract {search_contract!r}; ownership mismatches "
                f"{audit.ownership_mismatch_ids[:10]}, search-contract mismatches "
                f"{audit.search_contract_mismatch_ids[:10]}. Use fresh shadow names; "
                "no payloads were changed."
            )
        validated_ids[section] = (
            target,
            set(audit.ids),
            document_contract,
            search_contract,
        )

    # All collections were proven before the first write. This changes only
    # shadow ownership metadata; vectors and their search contracts are never
    # rewritten by migration.
    for target, ids, document_contract, _ in validated_ids.values():
        for batch_ids in _chunks(sorted(ids), 512):
            await qdrant.set_payload(
                collection_name=target,
                payload={SHADOW_CONTRACT_PAYLOAD: document_contract},
                points=cast(Any, list(batch_ids)),
                wait=True,
            )

    for target, ids, document_contract, search_contract in validated_ids.values():
        audit = await _audit_collection_points(
            qdrant,
            collection=target,
            build_id=build_id,
            document_contract=document_contract,
            search_index_contract=search_contract,
        )
        if (
            set(audit.ids) != ids
            or audit.ownership_mismatch_ids
            or audit.search_contract_mismatch_ids
        ):
            raise ValueError(
                f"schema-v3 ownership migration verification failed for {target!r}"
            )

    checkpoint["schema_version"] = CHECKPOINT_SCHEMA_VERSION
    checkpoint["document_contracts"] = {
        "positions": POSITION_DOCUMENT_CONTRACT,
        "institutions": (
            INSTITUTION_DOCUMENT_CONTRACT
            if institution_collection is not None
            else None
        ),
    }
    checkpoint.pop("document_contract", None)
    return as_of


async def validate_collection(
    qdrant: AsyncQdrantClient,
    *,
    collection: str,
    expected_ids: set[int],
    build_id: str | None = None,
    document_contract: str | None = None,
    search_index_contract: str | None = None,
) -> CollectionValidation:
    if not await qdrant.collection_exists(collection):
        raise ValueError(f"shadow collection {collection!r} does not exist")
    ownership_mismatch_ids: tuple[int, ...] = ()
    search_contract_mismatch_ids: tuple[int, ...] = ()
    if build_id is not None:
        if document_contract is None or search_index_contract is None:
            raise ValueError(
                "owned collection validation requires document and search contracts"
            )
        audit = await _audit_collection_points(
            qdrant,
            collection=collection,
            build_id=build_id,
            document_contract=document_contract,
            search_index_contract=search_index_contract,
        )
        actual_ids = set(audit.ids)
        ownership_mismatch_ids = audit.ownership_mismatch_ids
        search_contract_mismatch_ids = audit.search_contract_mismatch_ids
    else:
        actual_ids = await _collection_ids(qdrant, collection)
    info = await qdrant.get_collection(collection)
    actual_count = (await qdrant.count(collection, exact=True)).count
    return CollectionValidation(
        collection=collection,
        expected_count=len(expected_ids),
        actual_count=actual_count,
        dimension=_vector_dimension(info),
        missing_ids=tuple(sorted(expected_ids - actual_ids)),
        unexpected_ids=tuple(sorted(actual_ids - expected_ids)),
        ownership_mismatch_ids=ownership_mismatch_ids,
        search_contract_mismatch_ids=search_contract_mismatch_ids,
    )


async def _load_position_snapshot(
    session: AsyncSession,
    *,
    as_of: date,
) -> tuple[list[PositionRow], FamilyFeedbackProfiles]:
    family_profiles = await _load_family_feedback_profiles(session)
    broad_rows = (await session.execute(_position_candidate_statement(as_of))).all()
    rows = [
        (row[0], row[1], row[2])
        for row in broad_rows
        if _verification_metadata(row[0], as_of, listing_page=row[2]) is not None
    ]
    return rows, family_profiles


async def _load_institution_snapshot(
    session: AsyncSession,
    *,
    as_of: date,
) -> list[dict[str, object]]:
    universities = list(
        (
            await session.execute(select(University).order_by(University.id))
        ).scalars().all()
    )
    rows = (await session.execute(_institution_position_statement(as_of))).all()
    return _build_entities(
        universities,
        [(row[0], row[1]) for row in rows],
        today=as_of,
    )


async def load_read_only_snapshot(
    settings: Settings,
    *,
    as_of: date,
    include_institutions: bool,
) -> tuple[list[PositionRow], FamilyFeedbackProfiles, list[dict[str, object]]]:
    """Materialize one DB snapshot, rolling back the read-only transaction."""
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
                await connection.execute(text(READ_ONLY_SNAPSHOT_SQL))
                session = AsyncSession(
                    bind=connection,
                    expire_on_commit=False,
                    autoflush=False,
                )
                try:
                    rows, profiles = await _load_position_snapshot(
                        session,
                        as_of=as_of,
                    )
                    entities = (
                        await _load_institution_snapshot(session, as_of=as_of)
                        if include_institutions
                        else []
                    )
                finally:
                    await session.close()
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
    return rows, profiles, entities


def _new_checkpoint(
    *,
    collection: str,
    institution_collection: str | None,
    embedding_identity: EmbeddingIdentity,
    embedding_dimension: int,
    as_of: date,
) -> dict[str, Any]:
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "document_contracts": {
            "positions": POSITION_DOCUMENT_CONTRACT,
            "institutions": (
                INSTITUTION_DOCUMENT_CONTRACT
                if institution_collection is not None
                else None
            ),
        },
        "embedding_profile": embedding_identity.profile,
        "embedding": embedding_identity.checkpoint_payload(),
        "embedding_dimension": embedding_dimension,
        "collection": collection,
        "institution_collection": institution_collection,
        "build_id": str(uuid4()),
        "as_of": as_of.isoformat(),
        "status": "building",
        "positions": {},
        "institutions": {},
    }


def _set_snapshot_contract(
    checkpoint: dict[str, Any],
    *,
    section: str,
    fingerprint: str,
    expected_count: int,
) -> None:
    existing = checkpoint.get(section)
    if isinstance(existing, dict) and existing.get("fingerprint") is not None:
        if existing.get("fingerprint") != fingerprint:
            raise ValueError(
                f"{section} database snapshot changed since the interrupted build; "
                "use a fresh shadow collection name"
            )
        if existing.get("expected_count") != expected_count:
            raise ValueError(f"{section} expected count changed despite matching hash")
        return
    checkpoint[section] = {
        "fingerprint": fingerprint,
        "expected_count": expected_count,
        "completed": 0,
        "status": "building",
    }


async def _build_positions(
    *,
    qdrant: AsyncQdrantClient,
    model: ModelHelper,
    collection: str,
    rows: Sequence[PositionRow],
    family_profiles: FamilyFeedbackProfiles,
    as_of: date,
    batch_size: int,
    checkpoint: dict[str, Any],
    checkpoint_path: Path,
) -> CollectionValidation:
    expected_ids = {position.id for position, _, _ in rows}
    build_id = str(checkpoint["build_id"])
    search_index_contract = model.search_index_contract()
    inflight = {
        int(position_id)
        for position_id in checkpoint["positions"].get("inflight_ids", [])
    }
    existing_ids = await _trusted_collection_ids(
        qdrant,
        collection=collection,
        build_id=build_id,
        document_contract=POSITION_DOCUMENT_CONTRACT,
        search_index_contract=search_index_contract,
        allowed_unmarked_ids=inflight,
    )
    unexpected = existing_ids - expected_ids
    if unexpected:
        raise ValueError(
            f"shadow position collection contains {len(unexpected)} unexpected IDs; "
            "use a fresh collection name"
        )
    missing_rows = [row for row in rows if row[0].id not in existing_ids]
    for batch in _chunks(missing_rows, batch_size):
        batch_ids = [position.id for position, _, _ in batch]
        checkpoint["positions"]["inflight_ids"] = batch_ids
        write_checkpoint(checkpoint_path, checkpoint)
        await _embed_and_upsert(
            model,
            qdrant,
            collection,
            list(batch),
            family_profiles=family_profiles,
            today=as_of,
        )
        await qdrant.set_payload(
            collection_name=collection,
            payload={
                SHADOW_BUILD_ID_PAYLOAD: build_id,
                SHADOW_CONTRACT_PAYLOAD: POSITION_DOCUMENT_CONTRACT,
            },
            points=cast(Any, batch_ids),
            wait=True,
        )
        existing_ids.update(batch_ids)
        checkpoint["positions"]["completed"] = len(existing_ids)
        checkpoint["positions"]["inflight_ids"] = []
        write_checkpoint(checkpoint_path, checkpoint)
        print(f"positions: {len(existing_ids)}/{len(expected_ids)}")
    validation = await validate_collection(
        qdrant,
        collection=collection,
        expected_ids=expected_ids,
        build_id=build_id,
        document_contract=POSITION_DOCUMENT_CONTRACT,
        search_index_contract=search_index_contract,
    )
    checkpoint["positions"].update(
        {
            "completed": validation.actual_count,
            "dimension": validation.dimension,
            "status": "complete" if validation.valid else "invalid",
        }
    )
    write_checkpoint(checkpoint_path, checkpoint)
    return validation


async def _build_institutions(
    *,
    qdrant: AsyncQdrantClient,
    model: ModelHelper,
    collection: str,
    entities: Sequence[dict[str, object]],
    batch_size: int,
    checkpoint: dict[str, Any],
    checkpoint_path: Path,
) -> CollectionValidation:
    expected_ids = {
        cast(int, entity["id"])
        for entity in entities
        if isinstance(entity.get("id"), int)
    }
    if len(expected_ids) != len(entities):
        raise ValueError("institution snapshot contains missing or duplicate integer IDs")
    build_id = str(checkpoint["build_id"])
    search_index_contract = model.search_index_contract(institutions=True)
    inflight = {
        int(entity_id)
        for entity_id in checkpoint["institutions"].get("inflight_ids", [])
    }
    existing_ids = await _trusted_collection_ids(
        qdrant,
        collection=collection,
        build_id=build_id,
        document_contract=INSTITUTION_DOCUMENT_CONTRACT,
        search_index_contract=search_index_contract,
        allowed_unmarked_ids=inflight,
    )
    unexpected = existing_ids - expected_ids
    if unexpected:
        raise ValueError(
            f"shadow institution collection contains {len(unexpected)} unexpected IDs; "
            "use a fresh collection name"
        )
    missing_entities = [
        entity for entity in entities if cast(int, entity["id"]) not in existing_ids
    ]
    for batch in _chunks(missing_entities, batch_size):
        batch_ids = [cast(int, entity["id"]) for entity in batch]
        checkpoint["institutions"]["inflight_ids"] = batch_ids
        write_checkpoint(checkpoint_path, checkpoint)
        documents = [
            build_institution_search_document(
                name=str(entity["name"]),
                university=str(entity["university"]),
                kind=str(entity["kind"]),
                text=str(entity["text"]),
            )
            for entity in batch
        ]
        vectors = await model.embed_documents(documents)
        if not vectors:
            continue
        if not await qdrant.collection_exists(collection):
            await qdrant.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(
                    size=len(vectors[0]),
                    distance=Distance.COSINE,
                ),
            )
        await qdrant.upsert(
            collection_name=collection,
            points=[
                PointStruct(
                    id=cast(int, entity["id"]),
                    vector=vector,
                    payload={
                        **{
                            key: value
                            for key, value in entity.items()
                            if key not in ("id", "text")
                        },
                        SHADOW_BUILD_ID_PAYLOAD: build_id,
                        SHADOW_CONTRACT_PAYLOAD: INSTITUTION_DOCUMENT_CONTRACT,
                        SEARCH_INDEX_CONTRACT_PAYLOAD: search_index_contract,
                    },
                )
                for entity, vector in zip(batch, vectors, strict=True)
            ],
            wait=True,
        )
        existing_ids.update(batch_ids)
        checkpoint["institutions"]["completed"] = len(existing_ids)
        checkpoint["institutions"]["inflight_ids"] = []
        write_checkpoint(checkpoint_path, checkpoint)
        print(f"institutions: {len(existing_ids)}/{len(expected_ids)}")
    validation = await validate_collection(
        qdrant,
        collection=collection,
        expected_ids=expected_ids,
        build_id=build_id,
        document_contract=INSTITUTION_DOCUMENT_CONTRACT,
        search_index_contract=search_index_contract,
    )
    checkpoint["institutions"].update(
        {
            "completed": validation.actual_count,
            "dimension": validation.dimension,
            "status": "complete" if validation.valid else "invalid",
        }
    )
    write_checkpoint(checkpoint_path, checkpoint)
    return validation


async def run_shadow(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    validate_target_names(
        production_collection=settings.qdrant.collection,
        collection=args.collection,
        institution_collection=args.institution_collection,
    )
    checkpoint_path = _checkpoint_path(args.collection, args.checkpoint)
    embedding_config = embedding_config_for_run(
        settings.embedding,
        getattr(args, "embedding_model", None),
    )
    qdrant = AsyncQdrantClient(url=settings.qdrant.url)
    try:
        destination_names = [args.collection]
        if args.institution_collection:
            destination_names.append(args.institution_collection)
        existing = {
            name for name in destination_names if await qdrant.collection_exists(name)
        }

        if not args.resume and existing:
            raise ValueError(
                "destination already exists; refusing to modify it without a "
                f"matching checkpoint and --resume: {sorted(existing)}"
            )

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as identity_client:
            embedding_identity = await resolve_embedding_identity(
                embedding_config,
                client=identity_client,
            )
        model = ModelHelper(settings.llm, embedding_config)
        embedding_dimension = await resolve_embedding_dimension(model)

        if args.resume:
            if not checkpoint_path.exists():
                raise ValueError(
                    f"resume requires this command's checkpoint: {checkpoint_path}"
                )
            checkpoint = read_checkpoint(checkpoint_path)
            if checkpoint.get("schema_version") == LEGACY_CHECKPOINT_SCHEMA_VERSION:
                await migrate_completed_v3_checkpoint(
                    checkpoint,
                    qdrant=qdrant,
                    model=model,
                    collection=args.collection,
                    institution_collection=args.institution_collection,
                    embedding_identity=embedding_identity,
                    embedding_dimension=embedding_dimension,
                )
                write_checkpoint(checkpoint_path, checkpoint)
            as_of = validate_checkpoint_contract(
                checkpoint,
                collection=args.collection,
                institution_collection=args.institution_collection,
                embedding_identity=embedding_identity,
                embedding_dimension=embedding_dimension,
            )
            # This guard deliberately runs before the read-only snapshot and,
            # most importantly, before either builder can call upsert.
            await validate_existing_collection_dimensions(
                qdrant,
                collections=destination_names,
                expected_dimension=embedding_dimension,
            )
        else:
            if checkpoint_path.exists():
                raise ValueError(
                    f"checkpoint already exists at {checkpoint_path}; use --resume "
                    "or choose a fresh collection name"
                )
            as_of = local_today()
            checkpoint = _new_checkpoint(
                collection=args.collection,
                institution_collection=args.institution_collection,
                embedding_identity=embedding_identity,
                embedding_dimension=embedding_dimension,
                as_of=as_of,
            )
            write_checkpoint(checkpoint_path, checkpoint)

        rows, family_profiles, entities = await load_read_only_snapshot(
            settings,
            as_of=as_of,
            include_institutions=args.institution_collection is not None,
        )
        if not rows:
            raise ValueError("no currently searchable positions were found")

        position_fingerprint = position_snapshot_fingerprint(
            rows,
            family_profiles=family_profiles,
        )
        _set_snapshot_contract(
            checkpoint,
            section="positions",
            fingerprint=position_fingerprint,
            expected_count=len(rows),
        )
        if args.institution_collection:
            institution_fingerprint = institution_snapshot_fingerprint(entities)
            _set_snapshot_contract(
                checkpoint,
                section="institutions",
                fingerprint=institution_fingerprint,
                expected_count=len(entities),
            )
        write_checkpoint(checkpoint_path, checkpoint)

        position_validation = await _build_positions(
            qdrant=qdrant,
            model=model,
            collection=args.collection,
            rows=rows,
            family_profiles=family_profiles,
            as_of=as_of,
            batch_size=args.batch,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
        )

        institution_validation: CollectionValidation | None = None
        if args.institution_collection:
            if not entities:
                raise ValueError("no institution entities were found")
            institution_validation = await _build_institutions(
                qdrant=qdrant,
                model=model,
                collection=args.institution_collection,
                entities=entities,
                batch_size=args.batch,
                checkpoint=checkpoint,
                checkpoint_path=checkpoint_path,
            )

        valid = position_validation.valid and (
            institution_validation is None or institution_validation.valid
        )
        checkpoint["status"] = "complete" if valid else "invalid"
        write_checkpoint(checkpoint_path, checkpoint)
        report = {
            "valid": valid,
            "as_of": as_of.isoformat(),
            "embedding": embedding_identity.checkpoint_payload(),
            "embedding_profile": embedding_identity.profile,
            "embedding_dimension": embedding_dimension,
            "document_contracts": {
                "positions": POSITION_DOCUMENT_CONTRACT,
                "institutions": (
                    INSTITUTION_DOCUMENT_CONTRACT
                    if args.institution_collection is not None
                    else None
                ),
            },
            "checkpoint": str(checkpoint_path),
            "production_collection_untouched": settings.qdrant.collection,
            "positions": {
                "collection": position_validation.collection,
                "expected_count": position_validation.expected_count,
                "actual_count": position_validation.actual_count,
                "dimension": position_validation.dimension,
                "missing_ids": list(position_validation.missing_ids),
                "unexpected_ids": list(position_validation.unexpected_ids),
                "ownership_mismatch_ids": list(
                    position_validation.ownership_mismatch_ids
                ),
                "search_contract_mismatch_ids": list(
                    position_validation.search_contract_mismatch_ids
                ),
            },
            "institutions": (
                {
                    "collection": institution_validation.collection,
                    "expected_count": institution_validation.expected_count,
                    "actual_count": institution_validation.actual_count,
                    "dimension": institution_validation.dimension,
                    "missing_ids": list(institution_validation.missing_ids),
                    "unexpected_ids": list(institution_validation.unexpected_ids),
                    "ownership_mismatch_ids": list(
                        institution_validation.ownership_mismatch_ids
                    ),
                    "search_contract_mismatch_ids": list(
                        institution_validation.search_contract_mismatch_ids
                    ),
                }
                if institution_validation is not None
                else {
                    "skipped": True,
                    "reason": "pass --institution-collection to build it explicitly",
                }
            ),
        }
        return report
    finally:
        await qdrant.close()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a resumable shadow Qdrant search index while keeping "
            "PostgreSQL and production collections untouched."
        )
    )
    parser.add_argument(
        "--collection",
        required=True,
        help="new, explicit Qdrant collection for opportunity vectors",
    )
    parser.add_argument(
        "--institution-collection",
        help="new, explicit Qdrant collection for institution/group vectors",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help="checkpoint path (default: var/shadow-index/<collection>.json)",
    )
    parser.add_argument(
        "--embedding-model",
        help=(
            "exact LiteLLM embedding model for this shadow build only; retains "
            "the configured API base/key and is pinned in the checkpoint"
        ),
    )
    parser.add_argument(
        "--batch",
        type=_positive_int,
        default=DEFAULT_BATCH_SIZE,
        help=f"embedding batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume only when the matching checkpoint and DB snapshot agree",
    )
    return parser


async def _main() -> int:
    load_dotenv()
    args = build_parser().parse_args()
    settings = Settings()
    try:
        report = await run_shadow(args, settings)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"shadow rebuild failed safely: {type(error).__name__}: {error}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
