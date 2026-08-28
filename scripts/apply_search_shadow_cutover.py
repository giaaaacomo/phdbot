"""Guarded PostgreSQL membership alignment for a validated search shadow.

This command deliberately does *not* edit ``.env`` and does not start, stop, or
restart services.  The API must already be down.  It consumes the exact JSON
written by :mod:`scripts.plan_search_shadow_cutover`, verifies a fresh custom
PostgreSQL backup, rechecks the database and every relevant Qdrant collection,
and only then aligns ``positions.indexed_at`` with the shadow membership in one
serializable transaction.

The recovery artifact is local operational state.  Put it below ``var/`` (the
repository ignores that directory) and retain it together with the backup and
the preflight manifest until the cutover has been accepted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from phd_searcher.clock import local_today
from phd_searcher.config import Settings
from phd_searcher.database.models.position import Position
from phd_searcher.engine.model_helper import (
    EMBEDDING_INPUT_CONTRACT_VERSION,
    embedding_profile_for_model,
)
from phd_searcher.engine.search_documents import (
    CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
    INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
)
from scripts.plan_search_shadow_cutover import (
    CHECKPOINT_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    CollectionAudit,
    DatabaseSnapshot,
    _materialize_database_snapshot,
    audit_collection,
    id_set_sha256,
    resolve_model_identity,
)

PIPELINE_ADVISORY_LOCK_KEY = 918_273_645
RECOVERY_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^(?:sha256:)?(?P<hex>[0-9a-f]{64})$", re.I)
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$", re.I)


class CutoverRefused(RuntimeError):  # noqa: N818 - operational refusal, not an internal failure
    """Raised before commit whenever a cutover invariant cannot be proved."""


@dataclass(frozen=True, slots=True)
class CutoverExpectations:
    production_collection: str
    position_collection: str
    institution_collection: str
    embedding_model: str
    embedding_digest: str


@dataclass(frozen=True, slots=True)
class ValidatedManifest:
    generated_at: datetime
    as_of: date
    production_collection: str
    position_collection: str
    institution_collection: str
    embedding_model: str
    embedding_digest: str
    embedding_dimension: int
    position_contract: str
    institution_contract: str
    shadow_build_id: str
    safe_position_ids: frozenset[int]
    safe_institution_ids: frozenset[int]
    indexed_membership_count: int
    indexed_membership_sha256: str
    position_fingerprint: str
    institution_fingerprint: str
    to_set: frozenset[int]
    to_clear: frozenset[int]


@dataclass(frozen=True, slots=True)
class BackupEvidence:
    path: Path
    size: int
    modified_at: datetime
    sha256: str
    verifier: tuple[str, ...]
    listing_entries: int

    def report(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "size": self.size,
            "modified_at": self.modified_at.isoformat(),
            "sha256": self.sha256,
            "verifier": list(self.verifier),
            "listing_entries": self.listing_entries,
        }


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise CutoverRefused(f"manifest {label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CutoverRefused(f"manifest {label} must be a non-empty string")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CutoverRefused(f"manifest {label} must be an integer >= {minimum}")
    return value


def _sha256(value: object, label: str) -> str:
    raw = _string(value, label)
    match = _SHA256.fullmatch(raw)
    if match is None:
        raise CutoverRefused(f"manifest {label} must be an exact SHA-256 digest")
    return f"sha256:{match.group('hex').lower()}"


def _fingerprint(value: object, label: str) -> str:
    raw = _string(value, label).lower()
    if _FINGERPRINT.fullmatch(raw) is None:
        raise CutoverRefused(f"manifest {label} must be a bare SHA-256 fingerprint")
    return raw


def _ids(value: object, label: str) -> frozenset[int]:
    if not isinstance(value, list):
        raise CutoverRefused(f"manifest {label} must be an integer list")
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in value):
        raise CutoverRefused(f"manifest {label} contains an invalid ID")
    if value != sorted(value) or len(value) != len(set(value)):
        raise CutoverRefused(f"manifest {label} must be sorted and unique")
    return frozenset(value)


def _parse_datetime(value: object, label: str) -> datetime:
    raw = _string(value, label)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise CutoverRefused(f"manifest {label} is not ISO-8601") from error
    if parsed.tzinfo is None:
        raise CutoverRefused(f"manifest {label} must include a timezone")
    return parsed.astimezone(UTC)


def _parse_date(value: object, label: str) -> date:
    raw = _string(value, label)
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise CutoverRefused(f"manifest {label} is not an ISO date") from error


def _expected_contract(model: str, *, institutions: bool) -> str:
    configured = model.strip().casefold()
    profile = embedding_profile_for_model(configured)
    document = (
        INSTITUTION_SEARCH_DOCUMENT_CONTRACT
        if institutions
        else CANDIDATE_SEARCH_DOCUMENT_CONTRACT
    )
    return f"{document}|{EMBEDDING_INPUT_CONTRACT_VERSION[profile]}|{configured}"


def _require_single_value_report(
    report: object,
    *,
    label: str,
    value: str,
    count: int,
) -> None:
    if report != [{"value": value, "count": count}]:
        raise CutoverRefused(f"manifest {label} does not pin every point exactly")


def _validate_collection_health(
    *,
    status: object,
    optimizer_status: object,
    optimizer_error: object,
    distance: object,
    label: str,
) -> None:
    if status != "green":
        raise CutoverRefused(f"{label} is not green")
    if optimizer_status is not None and optimizer_status != "ok":
        detail = f": {optimizer_error}" if isinstance(optimizer_error, str) else ""
        raise CutoverRefused(f"{label} optimizer is not OK{detail}")
    if distance != Distance.COSINE.value:
        raise CutoverRefused(f"{label} does not use unnamed Cosine vectors")


def _validate_collection_report(
    report: object,
    *,
    label: str,
    collection: str,
    ids: frozenset[int],
    dimension: int,
    search_contract: str,
    build_id: str,
    document_contract: str,
) -> None:
    data = _mapping(report, label)
    count = len(ids)
    if data.get("collection") != collection or data.get("exists") is not True:
        raise CutoverRefused(f"manifest {label} does not identify the expected collection")
    if data.get("exact_count") != count or data.get("integer_id_count") != count:
        raise CutoverRefused(f"manifest {label} count does not match its safe IDs")
    if data.get("integer_ids_sha256") != id_set_sha256(ids):
        raise CutoverRefused(f"manifest {label} ID digest does not match its safe IDs")
    if data.get("non_integer_ids") != [] or data.get("dimension") != dimension:
        raise CutoverRefused(f"manifest {label} has invalid IDs or vector dimension")
    _validate_collection_health(
        status=data.get("status"),
        optimizer_status=data.get("optimizer_status"),
        optimizer_error=data.get("optimizer_error"),
        distance=data.get("distance"),
        label=f"manifest {label}",
    )
    _require_single_value_report(
        data.get("search_index_contracts"),
        label=f"{label}.search_index_contracts",
        value=search_contract,
        count=count,
    )
    _require_single_value_report(
        data.get("shadow_build_ids"),
        label=f"{label}.shadow_build_ids",
        value=build_id,
        count=count,
    )
    _require_single_value_report(
        data.get("shadow_document_contracts"),
        label=f"{label}.shadow_document_contracts",
        value=document_contract,
        count=count,
    )


def validate_manifest(
    payload: object,
    expectations: CutoverExpectations,
) -> ValidatedManifest:
    """Validate all redundant manifest claims; never trust names from JSON alone."""
    root = _mapping(payload, "root")
    if root.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise CutoverRefused("unsupported preflight manifest schema")
    if root.get("read_only") is not True or root.get("ready_for_atomic_cutover") is not True:
        raise CutoverRefused("preflight manifest is not a ready read-only plan")
    if root.get("blockers") != []:
        raise CutoverRefused("preflight manifest contains blockers")

    expected_digest = _sha256(expectations.embedding_digest, "expected embedding digest")
    if expectations.institution_collection != f"{expectations.position_collection}_institutions":
        raise CutoverRefused("institution target must be the position target plus '_institutions'")
    if expectations.position_collection in {
        expectations.production_collection,
        f"{expectations.production_collection}_institutions",
    }:
        raise CutoverRefused("shadow position target overlaps production")

    requested = _mapping(root.get("requested"), "requested")
    manifest = _mapping(root.get("cutover_manifest"), "cutover_manifest")
    model = _mapping(root.get("model"), "model")
    checkpoint = _mapping(root.get("checkpoint"), "checkpoint")
    database = _mapping(root.get("database_snapshot"), "database_snapshot")
    collections = _mapping(root.get("collections"), "collections")
    reconciliation = _mapping(root.get("reconciliation"), "reconciliation")
    position_reconciliation = _mapping(reconciliation.get("positions"), "reconciliation.positions")
    institution_reconciliation = _mapping(
        reconciliation.get("institutions"), "reconciliation.institutions"
    )

    exact_claims = (
        (requested.get("production_collection"), expectations.production_collection, "requested production"),
        (requested.get("position_collection"), expectations.position_collection, "requested position target"),
        (requested.get("institution_collection"), expectations.institution_collection, "requested institution target"),
        (requested.get("embedding_model"), expectations.embedding_model, "requested embedding model"),
        (manifest.get("position_collection"), expectations.position_collection, "cutover position target"),
        (manifest.get("institution_collection"), expectations.institution_collection, "cutover institution target"),
        (manifest.get("embedding_model"), expectations.embedding_model, "cutover embedding model"),
        (model.get("configured_model"), expectations.embedding_model, "model identity"),
        (checkpoint.get("collection"), expectations.position_collection, "checkpoint position target"),
        (checkpoint.get("institution_collection"), expectations.institution_collection, "checkpoint institution target"),
    )
    for actual, expected, label in exact_claims:
        if actual != expected:
            raise CutoverRefused(f"manifest {label} mismatch")
    if manifest.get("backup_required_before_mutation") is not True:
        raise CutoverRefused("manifest does not require a pre-mutation backup")
    if checkpoint.get("exists") is not True or checkpoint.get("status") != "complete":
        raise CutoverRefused("shadow checkpoint is not complete")
    if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise CutoverRefused("shadow checkpoint schema is not current")

    checkpoint_embedding = _mapping(checkpoint.get("embedding"), "checkpoint.embedding")
    for key in ("configured_model", "provider", "resolved_model", "digest"):
        if checkpoint_embedding.get(key) != model.get(key):
            raise CutoverRefused(f"checkpoint and model identity disagree on {key}")
    if checkpoint_embedding.get("configured_model") != expectations.embedding_model:
        raise CutoverRefused("checkpoint embedding model mismatch")
    for actual, label in (
        (manifest.get("embedding_digest"), "cutover embedding digest"),
        (model.get("digest"), "model digest"),
        (checkpoint_embedding.get("digest"), "checkpoint embedding digest"),
    ):
        if _sha256(actual, label) != expected_digest:
            raise CutoverRefused(f"manifest {label} mismatch")

    dimension = _integer(manifest.get("embedding_dimension"), "embedding_dimension", minimum=1)
    if (
        model.get("expected_dimension") != dimension
        or model.get("manifest_dimension") != dimension
        or checkpoint.get("embedding_dimension") != dimension
    ):
        raise CutoverRefused("manifest embedding dimensions disagree")
    expected_profile = embedding_profile_for_model(expectations.embedding_model)
    if (
        model.get("embedding_profile") != expected_profile
        or checkpoint.get("embedding_profile") != expected_profile
    ):
        raise CutoverRefused("manifest embedding profiles disagree")
    position_contract = _expected_contract(expectations.embedding_model, institutions=False)
    institution_contract = _expected_contract(expectations.embedding_model, institutions=True)
    if manifest.get("position_search_index_contract") != position_contract:
        raise CutoverRefused("position search contract mismatch")
    if manifest.get("institution_search_index_contract") != institution_contract:
        raise CutoverRefused("institution search contract mismatch")
    if model.get("position_search_index_contract") != position_contract:
        raise CutoverRefused("model position search contract mismatch")
    if model.get("institution_search_index_contract") != institution_contract:
        raise CutoverRefused("model institution search contract mismatch")

    document_contracts = _mapping(checkpoint.get("document_contracts"), "checkpoint.document_contracts")
    if document_contracts != {
        "positions": CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
        "institutions": INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
    }:
        raise CutoverRefused("checkpoint document contracts are not current")
    build_id = _string(checkpoint.get("build_id"), "checkpoint.build_id")

    safe_positions = _ids(manifest.get("safe_position_ids"), "safe_position_ids")
    safe_institutions = _ids(manifest.get("safe_institution_ids"), "safe_institution_ids")
    to_set = _ids(manifest.get("db_indexed_at_to_set_ids"), "db_indexed_at_to_set_ids")
    to_clear = _ids(manifest.get("db_indexed_at_to_clear_ids"), "db_indexed_at_to_clear_ids")
    if not to_set <= safe_positions or to_clear & safe_positions or to_set & to_clear:
        raise CutoverRefused("indexed_at deltas are inconsistent with the safe membership")
    if position_reconciliation.get("db_indexed_at_to_set_ids") != sorted(to_set):
        raise CutoverRefused("position set delta disagrees with reconciliation")
    if position_reconciliation.get("db_indexed_at_to_clear_ids") != sorted(to_clear):
        raise CutoverRefused("position clear delta disagrees with reconciliation")
    if position_reconciliation.get("safe_missing_from_shadow_ids") != []:
        raise CutoverRefused("shadow is missing safe position IDs")
    if position_reconciliation.get("shadow_not_safe_ids") != []:
        raise CutoverRefused("shadow contains unsafe position IDs")
    if institution_reconciliation.get("safe_missing_from_shadow_ids") != []:
        raise CutoverRefused("shadow is missing safe institution IDs")
    if institution_reconciliation.get("shadow_not_safe_ids") != []:
        raise CutoverRefused("shadow contains unsafe institution IDs")

    safe_count = _integer(database.get("safe_position_count"), "safe_position_count")
    institution_count = _integer(database.get("institution_count"), "institution_count")
    if safe_count != len(safe_positions) or database.get("safe_position_ids_sha256") != id_set_sha256(safe_positions):
        raise CutoverRefused("database safe position summary disagrees with manifest IDs")
    if institution_count != len(safe_institutions) or database.get("institution_ids_sha256") != id_set_sha256(safe_institutions):
        raise CutoverRefused("database institution summary disagrees with manifest IDs")
    indexed_count = _integer(
        database.get("indexed_at_membership_count"), "indexed_at_membership_count"
    )
    indexed_digest = _fingerprint(
        database.get("indexed_at_membership_sha256"), "indexed_at_membership_sha256"
    )
    position_fingerprint = _fingerprint(database.get("position_fingerprint"), "position_fingerprint")
    institution_fingerprint = _fingerprint(
        database.get("institution_fingerprint"), "institution_fingerprint"
    )
    as_of = _parse_date(database.get("as_of"), "database_snapshot.as_of")
    if checkpoint.get("as_of") != as_of.isoformat():
        raise CutoverRefused("checkpoint and database gate dates disagree")
    if database.get("isolation") != "REPEATABLE READ READ ONLY":
        raise CutoverRefused("database snapshot does not claim the required isolation")
    implied_indexed = (safe_positions - to_set) | to_clear
    if len(implied_indexed) != indexed_count or id_set_sha256(implied_indexed) != indexed_digest:
        raise CutoverRefused("indexed_at deltas do not reconstruct the planned database snapshot")

    _validate_collection_report(
        collections.get("positions"),
        label="collections.positions",
        collection=expectations.position_collection,
        ids=safe_positions,
        dimension=dimension,
        search_contract=position_contract,
        build_id=build_id,
        document_contract=CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
    )
    _validate_collection_report(
        collections.get("institutions"),
        label="collections.institutions",
        collection=expectations.institution_collection,
        ids=safe_institutions,
        dimension=dimension,
        search_contract=institution_contract,
        build_id=build_id,
        document_contract=INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
    )

    return ValidatedManifest(
        generated_at=_parse_datetime(root.get("generated_at"), "generated_at"),
        as_of=as_of,
        production_collection=expectations.production_collection,
        position_collection=expectations.position_collection,
        institution_collection=expectations.institution_collection,
        embedding_model=expectations.embedding_model,
        embedding_digest=expected_digest,
        embedding_dimension=dimension,
        position_contract=position_contract,
        institution_contract=institution_contract,
        shadow_build_id=build_id,
        safe_position_ids=safe_positions,
        safe_institution_ids=safe_institutions,
        indexed_membership_count=indexed_count,
        indexed_membership_sha256=indexed_digest,
        position_fingerprint=position_fingerprint,
        institution_fingerprint=institution_fingerprint,
        to_set=to_set,
        to_clear=to_clear,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup(
    path: Path,
    *,
    generated_at: datetime,
    max_age: timedelta,
) -> BackupEvidence:
    """Require a recent, listable PostgreSQL custom-format dump."""
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size <= 5:
        raise CutoverRefused("backup is not a non-empty regular file")
    with resolved.open("rb") as stream:
        if stream.read(5) != b"PGDMP":
            raise CutoverRefused("backup is not a PostgreSQL custom-format dump")
    modified = datetime.fromtimestamp(resolved.stat().st_mtime, tz=UTC)
    now = datetime.now(UTC)
    if modified > now + timedelta(minutes=5):
        raise CutoverRefused("backup modification time is in the future")
    if now - modified > max_age:
        raise CutoverRefused("backup is older than the permitted freshness window")
    if modified + timedelta(minutes=5) < generated_at:
        raise CutoverRefused("backup predates the preflight manifest")

    command: tuple[str, ...]
    pg_restore = shutil.which("pg_restore")
    if pg_restore:
        command = (pg_restore, "--list", str(resolved))
        result = subprocess.run(command, capture_output=True, check=False)
    else:
        docker = shutil.which("docker")
        if docker is None:
            raise CutoverRefused("neither pg_restore nor docker is available to verify the backup")
        command = (
            docker,
            "compose",
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "--list",
        )
        with resolved.open("rb") as stream:
            result = subprocess.run(command, stdin=stream, capture_output=True, check=False)
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise CutoverRefused(f"pg_restore could not list the backup: {stderr.strip()}")
    entries = sum(1 for line in stdout.splitlines() if line and not line.startswith(";"))
    if entries == 0:
        raise CutoverRefused("pg_restore reported no restorable entries")
    return BackupEvidence(
        path=resolved,
        size=resolved.stat().st_size,
        modified_at=modified,
        sha256=f"sha256:{_file_sha256(resolved)}",
        verifier=command,
        listing_entries=entries,
    )


def validate_database_snapshot(current: DatabaseSnapshot, plan: ValidatedManifest) -> None:
    if current.as_of != plan.as_of:
        raise CutoverRefused("database gate date differs from the preflight manifest")
    if current.position_ids != plan.safe_position_ids:
        raise CutoverRefused("current safe position membership differs from the preflight")
    if current.institution_ids != plan.safe_institution_ids:
        raise CutoverRefused("current institution membership differs from the preflight")
    if current.position_fingerprint != plan.position_fingerprint:
        raise CutoverRefused("current position fingerprint differs from the preflight")
    if current.institution_fingerprint != plan.institution_fingerprint:
        raise CutoverRefused("current institution fingerprint differs from the preflight")
    if len(current.indexed_position_ids) != plan.indexed_membership_count:
        raise CutoverRefused("current indexed_at membership count differs from the preflight")
    if id_set_sha256(current.indexed_position_ids) != plan.indexed_membership_sha256:
        raise CutoverRefused("current indexed_at membership digest differs from the preflight")
    if plan.to_set != plan.safe_position_ids - current.indexed_position_ids:
        raise CutoverRefused("set delta is stale")
    if plan.to_clear != current.indexed_position_ids - plan.safe_position_ids:
        raise CutoverRefused("clear delta is stale")
    if (current.indexed_position_ids | plan.to_set) - plan.to_clear != plan.safe_position_ids:
        raise CutoverRefused("deltas do not produce the exact safe membership")


def validate_shadow_audit(
    audit: CollectionAudit,
    *,
    collection: str,
    ids: frozenset[int],
    dimension: int,
    search_contract: str,
    build_id: str,
    document_contract: str,
) -> None:
    count = len(ids)
    if audit.collection != collection or not audit.exists:
        raise CutoverRefused(f"shadow collection {collection!r} is unavailable")
    if audit.non_integer_ids or audit.exact_count != count or audit.integer_ids != ids:
        raise CutoverRefused(f"shadow collection {collection!r} membership changed")
    if audit.dimension != dimension:
        raise CutoverRefused(f"shadow collection {collection!r} dimension changed")
    _validate_collection_health(
        status=audit.status,
        optimizer_status=audit.optimizer_status,
        optimizer_error=audit.optimizer_error,
        distance=audit.distance,
        label=f"shadow collection {collection!r}",
    )
    if audit.search_contracts != ((search_contract, count),):
        raise CutoverRefused(f"shadow collection {collection!r} search contract changed")
    if audit.build_ids != ((build_id, count),):
        raise CutoverRefused(f"shadow collection {collection!r} ownership changed")
    if audit.shadow_contracts != ((document_contract, count),):
        raise CutoverRefused(f"shadow collection {collection!r} document contract changed")


def validate_production_audits(
    positions: CollectionAudit,
    institutions: CollectionAudit,
    *,
    production_collection: str,
    indexed_ids: frozenset[int],
) -> None:
    if positions.collection != production_collection or not positions.exists:
        raise CutoverRefused("production rollback position collection is unavailable")
    if positions.non_integer_ids or positions.exact_count != len(positions.integer_ids):
        raise CutoverRefused("production rollback position collection has invalid IDs")
    _validate_collection_health(
        status=positions.status,
        optimizer_status=positions.optimizer_status,
        optimizer_error=positions.optimizer_error,
        distance=positions.distance,
        label="production rollback position collection",
    )
    if positions.integer_ids != indexed_ids:
        raise CutoverRefused("production rollback position membership differs from indexed_at")
    expected_institutions = f"{production_collection}_institutions"
    if institutions.collection != expected_institutions or not institutions.exists:
        raise CutoverRefused("production rollback institution collection is unavailable")
    if institutions.non_integer_ids or institutions.exact_count != len(institutions.integer_ids):
        raise CutoverRefused("production rollback institution collection has invalid IDs")
    _validate_collection_health(
        status=institutions.status,
        optimizer_status=institutions.optimizer_status,
        optimizer_error=institutions.optimizer_error,
        distance=institutions.distance,
        label="production rollback institution collection",
    )


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


async def _require_api_down(url: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.get(url)
    except httpx.ConnectError:
        return
    except httpx.HTTPError as error:
        raise CutoverRefused(f"cannot prove that the API is down: {error}") from error
    raise CutoverRefused(f"API is still reachable at {url} (HTTP {response.status_code})")


def _audit_report_with_ids(audit: CollectionAudit) -> dict[str, object]:
    return {**audit.report(), "integer_ids": sorted(audit.integer_ids)}


async def apply_cutover(
    *,
    settings: Settings,
    manifest_path: Path,
    expectations: CutoverExpectations,
    backup_path: Path,
    recovery_path: Path,
    max_backup_age: timedelta,
    api_health_url: str,
) -> dict[str, object]:
    if recovery_path.exists():
        raise CutoverRefused(f"recovery artifact already exists: {recovery_path}")
    raw_manifest = manifest_path.read_bytes()
    try:
        payload = json.loads(raw_manifest)
    except json.JSONDecodeError as error:
        raise CutoverRefused("preflight manifest is not valid JSON") from error
    plan = validate_manifest(payload, expectations)
    if local_today() != plan.as_of:
        raise CutoverRefused("preflight gate date is no longer today; generate a new manifest")
    if settings.qdrant.collection != plan.production_collection:
        raise CutoverRefused("configured production collection changed before cutover")
    if settings.embedding.model != plan.embedding_model:
        raise CutoverRefused("configured embedding model differs from the cutover model")

    await _require_api_down(api_health_url)
    backup = verify_backup(
        backup_path,
        generated_at=plan.generated_at,
        max_age=max_backup_age,
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        live_identity = await resolve_model_identity(settings.embedding, client=client)
    if live_identity.configured_model != plan.embedding_model:
        raise CutoverRefused("live embedding model name changed")
    if live_identity.digest != plan.embedding_digest:
        raise CutoverRefused("live embedding model digest changed")
    if live_identity.manifest_dimension != plan.embedding_dimension:
        raise CutoverRefused("live embedding model dimension changed")

    engine = create_async_engine(
        settings.database.url,
        echo=False,
        future=True,
        poolclass=NullPool,
        execution_options={"schema_translate_map": {None: settings.database.schema_name}},
    )
    recovery: dict[str, object] | None = None
    committed = False
    try:
        async with engine.connect() as base_connection:
            connection = await base_connection.execution_options(isolation_level="SERIALIZABLE")
            transaction = await connection.begin()
            try:
                got_lock = bool(
                    (
                        await connection.execute(
                            text("SELECT pg_try_advisory_xact_lock(:key)"),
                            {"key": PIPELINE_ADVISORY_LOCK_KEY},
                        )
                    ).scalar()
                )
                if not got_lock:
                    raise CutoverRefused("pipeline advisory lock is held; no mutation was attempted")
                quoted_schema = connection.dialect.identifier_preparer.quote(
                    settings.database.schema_name
                )
                await connection.execute(
                    text(
                        "LOCK TABLE "
                        f"{quoted_schema}.positions, "
                        f"{quoted_schema}.universities, "
                        f"{quoted_schema}.listing_pages "
                        "IN SHARE ROW EXCLUSIVE MODE"
                    )
                )
                session = AsyncSession(bind=connection, expire_on_commit=False, autoflush=False)
                try:
                    current = await _materialize_database_snapshot(session, as_of=plan.as_of)
                    validate_database_snapshot(current, plan)

                    qdrant = AsyncQdrantClient(url=settings.qdrant.url)
                    try:
                        (
                            target_positions,
                            target_institutions,
                            production_positions,
                            production_institutions,
                        ) = await asyncio.gather(
                            audit_collection(qdrant, plan.position_collection),
                            audit_collection(qdrant, plan.institution_collection),
                            audit_collection(qdrant, plan.production_collection),
                            audit_collection(
                                qdrant, f"{plan.production_collection}_institutions"
                            ),
                        )
                    finally:
                        await qdrant.close()
                    validate_shadow_audit(
                        target_positions,
                        collection=plan.position_collection,
                        ids=plan.safe_position_ids,
                        dimension=plan.embedding_dimension,
                        search_contract=plan.position_contract,
                        build_id=plan.shadow_build_id,
                        document_contract=CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
                    )
                    validate_shadow_audit(
                        target_institutions,
                        collection=plan.institution_collection,
                        ids=plan.safe_institution_ids,
                        dimension=plan.embedding_dimension,
                        search_contract=plan.institution_contract,
                        build_id=plan.shadow_build_id,
                        document_contract=INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
                    )
                    validate_production_audits(
                        production_positions,
                        production_institutions,
                        production_collection=plan.production_collection,
                        indexed_ids=current.indexed_position_ids,
                    )

                    old_rows = (
                        await session.execute(
                            select(Position.id, Position.indexed_at)
                            .where(Position.indexed_at.is_not(None))
                            .order_by(Position.id)
                        )
                    ).all()
                    cutover_at = datetime.now(UTC).replace(tzinfo=None)
                    recovery = {
                        "schema_version": RECOVERY_SCHEMA_VERSION,
                        "status": "commit_pending",
                        "created_at": datetime.now(UTC).isoformat(),
                        "manifest": {
                            "path": str(manifest_path.resolve()),
                            "sha256": f"sha256:{hashlib.sha256(raw_manifest).hexdigest()}",
                            "generated_at": plan.generated_at.isoformat(),
                        },
                        "backup": backup.report(),
                        "cutover_at": cutover_at.isoformat(),
                        "production_collection": plan.production_collection,
                        "target_collection": plan.position_collection,
                        "old_indexed_at": [
                            {"id": position_id, "value": indexed_at.isoformat()}
                            for position_id, indexed_at in old_rows
                        ],
                        "old_indexed_membership_sha256": id_set_sha256(
                            current.indexed_position_ids
                        ),
                        "target_indexed_membership_sha256": id_set_sha256(
                            plan.safe_position_ids
                        ),
                        "deltas": {
                            "set_ids": sorted(plan.to_set),
                            "clear_ids": sorted(plan.to_clear),
                        },
                        "production_qdrant": {
                            "positions": _audit_report_with_ids(production_positions),
                            "institutions": _audit_report_with_ids(
                                production_institutions
                            ),
                        },
                        "target_qdrant": {
                            "positions": target_positions.report(),
                            "institutions": target_institutions.report(),
                        },
                    }
                    _atomic_write_json(recovery_path, recovery)

                    set_count = 0
                    if plan.to_set:
                        result = await session.execute(
                            update(Position)
                            .where(
                                Position.id.in_(plan.to_set),
                                Position.indexed_at.is_(None),
                            )
                            .values(indexed_at=cutover_at)
                            .execution_options(synchronize_session=False)
                        )
                        set_count = int(getattr(result, "rowcount", -1))
                    clear_count = 0
                    if plan.to_clear:
                        result = await session.execute(
                            update(Position)
                            .where(
                                Position.id.in_(plan.to_clear),
                                Position.indexed_at.is_not(None),
                            )
                            .values(indexed_at=None)
                            .execution_options(synchronize_session=False)
                        )
                        clear_count = int(getattr(result, "rowcount", -1))
                    if set_count != len(plan.to_set) or clear_count != len(plan.to_clear):
                        raise CutoverRefused("indexed_at update row counts changed; transaction rolled back")
                    final_ids = frozenset(
                        (
                            await session.execute(
                                select(Position.id).where(Position.indexed_at.is_not(None))
                            )
                        ).scalars().all()
                    )
                    if final_ids != plan.safe_position_ids:
                        raise CutoverRefused("final indexed_at membership is not the safe shadow membership")
                    if id_set_sha256(final_ids) != id_set_sha256(plan.safe_position_ids):
                        raise CutoverRefused("final indexed_at membership digest mismatch")
                finally:
                    await session.close()
                await transaction.commit()
                committed = True
            except BaseException:
                if transaction.is_active:
                    await transaction.rollback()
                raise
    except BaseException as error:
        if recovery is not None and not committed:
            recovery = {**recovery, "status": "not_committed", "error": f"{type(error).__name__}: {error}"}
            _atomic_write_json(recovery_path, recovery)
        raise
    finally:
        await engine.dispose()

    assert recovery is not None
    recovery = {
        **recovery,
        "status": "committed",
        "committed_at": datetime.now(UTC).isoformat(),
    }
    _atomic_write_json(recovery_path, recovery)
    return recovery


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--recovery-output", type=Path, required=True)
    parser.add_argument("--expected-production-collection", required=True)
    parser.add_argument("--expected-position-collection", required=True)
    parser.add_argument("--expected-institution-collection", required=True)
    parser.add_argument("--expected-embedding-model", required=True)
    parser.add_argument("--expected-embedding-digest", required=True)
    parser.add_argument("--max-backup-age-hours", type=float, default=6.0)
    parser.add_argument("--api-health-url", default="http://127.0.0.1:8003/health")
    return parser


async def _async_main(args: argparse.Namespace) -> None:
    if args.max_backup_age_hours <= 0:
        raise CutoverRefused("max backup age must be positive")
    expectations = CutoverExpectations(
        production_collection=args.expected_production_collection,
        position_collection=args.expected_position_collection,
        institution_collection=args.expected_institution_collection,
        embedding_model=args.expected_embedding_model,
        embedding_digest=args.expected_embedding_digest,
    )
    result = await apply_cutover(
        settings=Settings(),
        manifest_path=args.manifest,
        expectations=expectations,
        backup_path=args.backup,
        recovery_path=args.recovery_output,
        max_backup_age=timedelta(hours=args.max_backup_age_hours),
        api_health_url=args.api_health_url,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "recovery_output": str(args.recovery_output),
                "target_collection": result["target_collection"],
                "target_indexed_membership_sha256": result[
                    "target_indexed_membership_sha256"
                ],
            },
            indent=2,
        )
    )


def main() -> None:
    load_dotenv()
    args = build_parser().parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
