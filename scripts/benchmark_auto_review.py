"""Read-only A/B benchmark for PHDBOT's production automatic-review contract.

The benchmark deliberately calls the same prompt, Ollama tool definition,
validation/feedback loop, confidence gates, and resilient batch splitting used
by the ``review`` pipeline stage. It never updates PostgreSQL or Qdrant.

Examples::

    uv run python scripts/benchmark_auto_review.py \
        --alternate-model ollama/qwen3.6:35b-a3b \
        --gold-set benchmarks/auto_review_gold.v1.json \
        --output /tmp/review-ab.json --markdown-output /tmp/review-ab.md

    uv run python scripts/benchmark_auto_review.py \
        --alternate-model qwen3.6:35b-a3b --id 148 --id 1210

Model tags are not downloaded by this script. Both models must already be
available to the configured local Ollama server.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from time import monotonic
from types import TracebackType
from typing import Any

import httpx
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import create_async_engine

from phd_searcher.config import Settings
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.opportunity_kinds import OPPORTUNITY_KINDS
from phd_searcher.pipeline import auto_review
from phd_searcher.position_types import POSITION_TYPES

DEFAULT_SAMPLE_SIZE = 12
DEFAULT_SEED = 20260826
DEFAULT_BATCH_SIZE = 8
DEFAULT_GOLD_SET = Path(__file__).resolve().parents[1] / "benchmarks" / "auto_review_gold.v1.json"
_GOLD_STATUSES = frozenset({"eligible", "rejected"})
_GOLD_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class Reference:
    """Stored PHDBOT state; useful for drift, but explicitly not ground truth."""

    screening_status: str
    screening_decision: str | None
    screening_source: str
    screening_manual: bool
    review_state: str
    position_type: str
    opportunity_kind: str


@dataclass(frozen=True, slots=True)
class Candidate:
    position: Position
    university: University | None
    reference: Reference

    @property
    def position_id(self) -> int:
        return self.position.id

    @property
    def evidence_band(self) -> str:
        document = auto_review._position_document(self.position)
        chars = len(" ".join(document.split()))
        if self.position.full_description is None:
            return "snippet"
        if chars < 500:
            return "short_detail"
        return "detail"

    @property
    def stratum(self) -> str:
        return ":".join(
            (
                self.reference.screening_status,
                self.reference.review_state,
                self.evidence_band,
                self.reference.position_type,
            )
        )

    @property
    def review_row(self) -> tuple[Position, University | None]:
        return self.position, self.university


@dataclass(frozen=True, slots=True)
class GoldLabel:
    """One human adjudication; ``review`` is an abstention, never a truth label."""

    position_id: int
    expected_status: str
    accepted_position_types: tuple[str, ...]
    accepted_opportunity_kinds: tuple[str, ...]
    content_sha256: str
    rationale: str


@dataclass(frozen=True, slots=True)
class GoldSet:
    name: str
    version: str
    labelled_at: str
    valid_until: date | None
    labels: tuple[GoldLabel, ...]
    source_path: Path
    schema_version: int = _GOLD_SCHEMA_VERSION

    @property
    def by_id(self) -> dict[int, GoldLabel]:
        return {label.position_id: label for label in self.labels}


@dataclass(frozen=True, slots=True)
class RequestTelemetry:
    position_ids: tuple[int, ...]
    status_code: int | None
    wall_seconds: float
    prompt_tokens: int
    completion_tokens: int
    total_duration_seconds: float
    load_duration_seconds: float
    prompt_eval_duration_seconds: float
    eval_duration_seconds: float
    tool_calls: int
    missing_tool_call: bool
    malformed_json: bool
    malformed_tool_shape: bool
    validation_feedback: str | None
    transport_error: str | None = None


@dataclass(slots=True)
class TelemetryRecorder:
    requests: list[RequestTelemetry] = field(default_factory=list)
    terminal_failures: list[str] = field(default_factory=list)
    request_exposures: Counter[int] = field(default_factory=Counter)
    _active_position_ids: tuple[int, ...] = field(default=(), repr=False)

    @contextmanager
    def candidate_scope(self, position_ids: Iterable[int]) -> Iterator[None]:
        previous = self._active_position_ids
        self._active_position_ids = tuple(dict.fromkeys(position_ids))
        try:
            yield
        finally:
            self._active_position_ids = previous

    def _request_position_ids(self, payload: dict[str, Any]) -> tuple[int, ...]:
        remaining_ids = _feedback_remaining_ids(payload)
        position_ids = self._active_position_ids if remaining_ids is None else remaining_ids
        self.request_exposures.update(position_ids)
        return position_ids

    def record_transport_error(
        self,
        payload: dict[str, Any],
        *,
        elapsed: float,
        error: Exception,
    ) -> None:
        position_ids = self._request_position_ids(payload)
        self.requests.append(
            RequestTelemetry(
                position_ids=position_ids,
                status_code=None,
                wall_seconds=elapsed,
                prompt_tokens=0,
                completion_tokens=0,
                total_duration_seconds=0.0,
                load_duration_seconds=0.0,
                prompt_eval_duration_seconds=0.0,
                eval_duration_seconds=0.0,
                tool_calls=0,
                missing_tool_call=False,
                malformed_json=False,
                malformed_tool_shape=False,
                validation_feedback=_feedback_detail(payload),
                transport_error=f"{type(error).__name__}: {str(error)[:300]}",
            )
        )

    def record_response(
        self,
        payload: dict[str, Any],
        response: httpx.Response,
        *,
        elapsed: float,
    ) -> None:
        position_ids = self._request_position_ids(payload)
        body: dict[str, Any] = {}
        malformed_response = False
        try:
            raw_body = response.json()
            if isinstance(raw_body, dict):
                body = raw_body
            else:
                malformed_response = True
        except (TypeError, ValueError, json.JSONDecodeError):
            malformed_response = True

        message = body.get("message")
        calls = list(message.get("tool_calls") or []) if isinstance(message, dict) else []
        malformed_json = False
        malformed_tool_shape = malformed_response
        for call in calls[:1]:
            try:
                auto_review._raw_tool_reviews(call)
            except json.JSONDecodeError:
                malformed_json = True
            except (TypeError, ValueError):
                malformed_tool_shape = True

        self.requests.append(
            RequestTelemetry(
                position_ids=position_ids,
                status_code=response.status_code,
                wall_seconds=elapsed,
                prompt_tokens=_non_negative_int(body.get("prompt_eval_count")),
                completion_tokens=_non_negative_int(body.get("eval_count")),
                total_duration_seconds=_nanoseconds_to_seconds(body.get("total_duration")),
                load_duration_seconds=_nanoseconds_to_seconds(body.get("load_duration")),
                prompt_eval_duration_seconds=_nanoseconds_to_seconds(body.get("prompt_eval_duration")),
                eval_duration_seconds=_nanoseconds_to_seconds(body.get("eval_duration")),
                tool_calls=len(calls),
                missing_tool_call=response.is_success and not calls and not malformed_response,
                malformed_json=malformed_json,
                malformed_tool_shape=malformed_tool_shape,
                validation_feedback=_feedback_detail(payload),
            )
        )


class _RecordingAsyncClient:
    def __init__(
        self,
        recorder: TelemetryRecorder,
        real_client: type[httpx.AsyncClient],
        **kwargs: object,
    ) -> None:
        self._recorder = recorder
        self._client = real_client(**kwargs)  # type: ignore[arg-type]

    async def __aenter__(self) -> _RecordingAsyncClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc_value, traceback)

    async def post(self, url: str, *, json: object, **kwargs: object) -> httpx.Response:
        payload = json if isinstance(json, dict) else {}
        started = monotonic()
        try:
            response = await self._client.post(url, json=json, **kwargs)  # type: ignore[arg-type]
        except Exception as exc:
            self._recorder.record_transport_error(
                payload,
                elapsed=monotonic() - started,
                error=exc,
            )
            raise
        self._recorder.record_response(
            payload,
            response,
            elapsed=monotonic() - started,
        )
        return response


class _HttpxFacade:
    """Module-shaped facade so instrumentation stays local to this script."""

    RequestError = httpx.RequestError

    def __init__(self, recorder: TelemetryRecorder, real_client: type[httpx.AsyncClient]) -> None:
        self._recorder = recorder
        self._real_client = real_client

    def AsyncClient(self, **kwargs: object) -> _RecordingAsyncClient:  # noqa: N802
        return _RecordingAsyncClient(self._recorder, self._real_client, **kwargs)


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and math.isfinite(value):
        return max(int(value), 0)
    return 0


def _nanoseconds_to_seconds(value: object) -> float:
    return _non_negative_int(value) / 1_000_000_000


def _feedback_detail(payload: dict[str, Any]) -> str | None:
    feedback = _latest_validation_feedback(payload)
    if feedback is None:
        return None
    return str(feedback.get("details") or "invalid_or_incomplete_reviews")[:1_200]


def _feedback_remaining_ids(payload: dict[str, Any]) -> tuple[int, ...] | None:
    feedback = _latest_validation_feedback(payload)
    if feedback is None:
        return None
    raw_ids = feedback.get("remaining_ids")
    if not isinstance(raw_ids, list) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 1
        for value in raw_ids
    ):
        return None
    return tuple(dict.fromkeys(raw_ids))


def _latest_validation_feedback(payload: dict[str, Any]) -> dict[str, Any] | None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") not in {"tool", "user"}:
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.lstrip().startswith("{"):
            continue
        try:
            feedback = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(feedback, dict) and feedback.get("error") == "invalid_or_incomplete_reviews":
            return feedback
    return None


@contextmanager
def _record_production_calls(recorder: TelemetryRecorder) -> Iterator[None]:
    """Observe production review calls without changing prompts or decisions."""
    real_httpx_module = auto_review.httpx  # type: ignore[attr-defined]
    real_native_review = auto_review._native_ollama_review
    real_client = httpx.AsyncClient

    async def recorded_native_review(
        settings: Settings,
        rows: list[tuple[Position, University | None]],
        *,
        max_attempts: int = 3,
    ) -> list[auto_review.AutomaticDecision]:
        try:
            with recorder.candidate_scope(position.id for position, _university in rows):
                return await real_native_review(settings, rows, max_attempts=max_attempts)
        except RuntimeError as exc:
            recorder.terminal_failures.append(str(exc)[:1_500])
            raise

    auto_review.httpx = _HttpxFacade(recorder, real_client)  # type: ignore[assignment,attr-defined]
    auto_review._native_ollama_review = recorded_native_review
    try:
        yield
    finally:
        auto_review.httpx = real_httpx_module  # type: ignore[attr-defined]
        auto_review._native_ollama_review = real_native_review


def normalize_ollama_model(model: str) -> str:
    value = model.strip()
    if not value:
        raise ValueError("model name cannot be empty")
    return value if value.startswith("ollama/") else f"ollama/{value}"


def load_gold_set(path: Path) -> GoldSet:
    """Load and strictly validate a versioned, human-labelled benchmark set."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load gold set {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != _GOLD_SCHEMA_VERSION:
        raise ValueError(
            f"gold set schema_version must be {_GOLD_SCHEMA_VERSION}; "
            "version 2 binds every prompt/evidence input field"
        )
    raw_labels = payload.get("labels")
    if not isinstance(raw_labels, list) or not raw_labels:
        raise ValueError("gold set labels must be a non-empty array")

    labels: list[GoldLabel] = []
    seen_ids: set[int] = set()
    for index, raw_label in enumerate(raw_labels):
        if not isinstance(raw_label, dict):
            raise ValueError(f"gold label {index} must be an object")
        position_id = raw_label.get("position_id")
        if not isinstance(position_id, int) or isinstance(position_id, bool) or position_id < 1:
            raise ValueError(f"gold label {index} has an invalid position_id")
        if position_id in seen_ids:
            raise ValueError(f"gold set contains duplicate position_id {position_id}")
        seen_ids.add(position_id)

        expected_status = raw_label.get("expected_status")
        if expected_status not in _GOLD_STATUSES:
            raise ValueError(
                f"gold label {position_id} expected_status must be eligible or rejected; "
                "review is a model abstention, not ground truth"
            )
        accepted_types = _validated_string_tuple(
            raw_label.get("accepted_position_types", []),
            allowed=set(POSITION_TYPES),
            field_name="accepted_position_types",
            position_id=position_id,
        )
        accepted_kinds = _validated_string_tuple(
            raw_label.get("accepted_opportunity_kinds", []),
            allowed=set(OPPORTUNITY_KINDS),
            field_name="accepted_opportunity_kinds",
            position_id=position_id,
        )
        if expected_status == "eligible" and (not accepted_types or not accepted_kinds):
            raise ValueError(
                f"eligible gold label {position_id} must define accepted position types and opportunity kinds"
            )
        content_sha256 = raw_label.get("content_sha256")
        if (
            not isinstance(content_sha256, str)
            or len(content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in content_sha256)
        ):
            raise ValueError(f"gold label {position_id} has an invalid content_sha256")
        rationale = raw_label.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(f"gold label {position_id} must include a rationale")
        labels.append(
            GoldLabel(
                position_id=position_id,
                expected_status=expected_status,
                accepted_position_types=accepted_types,
                accepted_opportunity_kinds=accepted_kinds,
                content_sha256=content_sha256,
                rationale=rationale.strip(),
            )
        )

    raw_valid_until = payload.get("valid_until")
    try:
        valid_until = date.fromisoformat(raw_valid_until) if raw_valid_until else None
    except (TypeError, ValueError) as exc:
        raise ValueError("gold set valid_until must use YYYY-MM-DD") from exc
    name = payload.get("name")
    version = payload.get("version")
    labelled_at = payload.get("labelled_at")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("gold set name must be a non-empty string")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("gold set version must be a non-empty string")
    if not isinstance(labelled_at, str) or not labelled_at.strip():
        raise ValueError("gold set name, version and labelled_at must be non-empty strings")
    return GoldSet(
        name=name,
        version=version,
        labelled_at=labelled_at,
        valid_until=valid_until,
        labels=tuple(labels),
        source_path=path,
        schema_version=_GOLD_SCHEMA_VERSION,
    )


def _validated_string_tuple(
    value: object,
    *,
    allowed: set[str],
    field_name: str,
    position_id: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"gold label {position_id} {field_name} must be an array of strings")
    result = tuple(dict.fromkeys(value))
    unknown = sorted(set(result) - allowed)
    if unknown:
        raise ValueError(f"gold label {position_id} {field_name} contains unknown values: {unknown}")
    return result


def candidate_content_sha256(candidate: Candidate) -> str:
    """Bind a judgement to every candidate field used by prompt/evidence construction."""
    position = candidate.position
    university = candidate.university
    payload = json.dumps(
        {
            "position_id": position.id,
            "title": position.title,
            "url": position.url,
            "description": position.description or "",
            "full_description": position.full_description or "",
            "position_type": position.position_type,
            "institution_name": position.institution_name,
            "institution_country": position.institution_country,
            "deadline": position.deadline.isoformat() if position.deadline is not None else None,
            "deadline_raw": position.deadline_raw,
            "university": (
                {"name": university.name, "country": university.country}
                if university is not None
                else None
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_gold_candidates(candidates: Sequence[Candidate], gold_set: GoldSet) -> None:
    by_id = {candidate.position_id: candidate for candidate in candidates}
    missing = [label.position_id for label in gold_set.labels if label.position_id not in by_id]
    if missing:
        raise ValueError(f"gold candidates are missing from the database: {missing}")
    changed = [
        label.position_id
        for label in gold_set.labels
        if candidate_content_sha256(by_id[label.position_id]) != label.content_sha256
    ]
    if changed:
        raise ValueError(
            "gold candidate content changed since human adjudication; relabel before benchmarking IDs: "
            f"{changed}"
        )
    if gold_set.valid_until is not None and date.today() > gold_set.valid_until:
        raise ValueError(
            f"gold set expired on {gold_set.valid_until.isoformat()}; refresh time-sensitive labels"
        )


def stratified_sample(
    candidates: Sequence[Candidate],
    *,
    sample_size: int,
    seed: int,
) -> list[Candidate]:
    """Round-robin deterministic strata instead of taking adjacent DB rows."""
    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    buckets: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        buckets[candidate.stratum].append(candidate)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: _stable_sample_key(item.position_id, seed))

    selected: list[Candidate] = []
    active = sorted(buckets)
    while active and len(selected) < sample_size:
        next_active: list[str] = []
        for stratum in active:
            bucket = buckets[stratum]
            if bucket and len(selected) < sample_size:
                selected.append(bucket.pop(0))
            if bucket:
                next_active.append(stratum)
        active = next_active
    return selected


def _stable_sample_key(position_id: int, seed: int) -> str:
    return hashlib.sha256(f"{seed}:{position_id}".encode()).hexdigest()


def _cohort_clause(cohort: str) -> str:
    if cohort == "review":
        return "p.screening_status = 'review' AND p.screening_manual IS FALSE"
    if cohort == "manual":
        return "p.screening_manual IS TRUE"
    if cohort == "all":
        return "TRUE"
    raise ValueError(f"unsupported cohort: {cohort}")


async def load_candidates(
    settings: Settings,
    *,
    ids: Sequence[int],
    cohort: str,
    sample_size: int,
    seed: int,
) -> list[Candidate]:
    """Load a fixed, attributable cohort inside a read-only transaction."""
    engine = create_async_engine(settings.database.url)
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            if ids:
                where = "p.id = ANY(:ids)"
                params: dict[str, object] = {"ids": list(dict.fromkeys(ids))}
                limit_clause = ""
            else:
                where = (
                    "p.is_active IS TRUE "
                    "AND (p.deadline IS NULL OR p.deadline >= CURRENT_DATE) "
                    f"AND {_cohort_clause(cohort)}"
                )
                params = {
                    "seed": seed,
                    "pool_limit": max(sample_size * 100, 1_000),
                }
                limit_clause = (
                    "ORDER BY md5(CAST(p.id AS TEXT) || ':' || CAST(:seed AS TEXT)) "
                    "LIMIT :pool_limit"
                )
            result = await connection.execute(
                text(
                    f"""
                    SELECT p.id, p.title, p.url, p.description, p.full_description,
                           p.position_type, p.opportunity_kind, p.institution_name,
                           p.institution_country, p.deadline, p.deadline_raw,
                           p.screening_status, p.screening_decision, p.screening_source,
                           p.screening_manual, p.review_state,
                           u.name AS university_name, u.country AS university_country
                    FROM positions p
                    LEFT JOIN universities u ON u.id = p.university_id
                    WHERE {where}
                    {limit_clause}
                    """
                ),
                params,
            )
            rows = result.mappings().all()
    finally:
        await engine.dispose()

    candidates = [_candidate_from_row(row) for row in rows]
    # The production stage routes these evidence-poor rows without an LLM. A
    # model benchmark must not charge them as model failures.
    model_candidates = [
        candidate
        for candidate in candidates
        if not auto_review._needs_evidence_bypass(candidate.position)
    ]
    if ids:
        by_id = {candidate.position_id: candidate for candidate in model_candidates}
        missing = [position_id for position_id in dict.fromkeys(ids) if position_id not in by_id]
        if missing:
            raise ValueError(
                "IDs missing or ineligible for model review (insufficient attributable text): "
                f"{missing}"
            )
        return [by_id[position_id] for position_id in dict.fromkeys(ids)]
    return stratified_sample(model_candidates, sample_size=sample_size, seed=seed)


def _candidate_from_row(row: RowMapping) -> Candidate:
    position = Position(
        id=row["id"],
        title=row["title"],
        url=row["url"],
        description=row["description"] or "",
        full_description=row["full_description"],
        position_type=row["position_type"],
        opportunity_kind=row["opportunity_kind"],
        institution_name=row["institution_name"],
        institution_country=row["institution_country"],
        deadline=row["deadline"],
        deadline_raw=row["deadline_raw"],
        screening_manual=row["screening_manual"],
    )
    university = None
    if row["university_name"]:
        university = University(
            wikidata_id=f"benchmark:{row['id']}",
            name=row["university_name"],
            country=row["university_country"] or row["institution_country"] or "XX",
            website_url="",
        )
    return Candidate(
        position=position,
        university=university,
        reference=Reference(
            screening_status=row["screening_status"],
            screening_decision=row["screening_decision"],
            screening_source=row["screening_source"],
            screening_manual=row["screening_manual"],
            review_state=row["review_state"],
            position_type=row["position_type"],
            opportunity_kind=row["opportunity_kind"],
        ),
    )


async def evaluate_model(
    settings: Settings,
    model: str,
    candidates: Sequence[Candidate],
    *,
    batch_size: int,
    gold_labels: dict[int, GoldLabel] | None = None,
) -> dict[str, Any]:
    model_settings = settings.model_copy(
        update={"llm": settings.llm.model_copy(update={"model": normalize_ollama_model(model)})}
    )
    warmup = await warm_up_model(model_settings, candidates[0])
    recorder = TelemetryRecorder()
    decisions: list[auto_review.AutomaticDecision] = []
    run_error: str | None = None
    started = monotonic()
    try:
        with _record_production_calls(recorder):
            for offset in range(0, len(candidates), batch_size):
                rows = [candidate.review_row for candidate in candidates[offset : offset + batch_size]]
                decisions.extend(await auto_review._resilient_ollama_review(model_settings, rows))
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        # A model-level incompatibility (for example unsupported tools or
        # thinking options) belongs in the comparison report; it must not erase
        # the baseline result or make the read-only benchmark itself fail.
        run_error = f"{type(exc).__name__}: {str(exc)[:1_500]}"
    wall_seconds = monotonic() - started
    by_candidate = {candidate.position_id: candidate for candidate in candidates}
    records: list[dict[str, Any]] = []
    for decision in decisions:
        candidate = by_candidate[decision.position_id]
        routed, needs_detail = auto_review._routed_status(
            decision,
            has_full_description=candidate.position.full_description is not None,
        )
        fallback = decision.reason.startswith("invalid model tool output:")
        records.append(
            {
                "position_id": decision.position_id,
                "title": candidate.position.title,
                "url": candidate.position.url,
                "decision": decision.decision,
                "routed_status": routed,
                "position_type": decision.position_type,
                "opportunity_kind": decision.opportunity_kind,
                "confidence": decision.confidence,
                "evidence": decision.evidence,
                "tool_attempts": decision.tool_attempts,
                "request_exposures": recorder.request_exposures.get(decision.position_id, 0),
                "latency_seconds": decision.latency_seconds,
                "contract_valid": not fallback,
                "needs_detail_before_reject": needs_detail,
                "reference_status": candidate.reference.screening_status,
                "reference_is_manual": candidate.reference.screening_manual,
            }
        )
    if gold_labels:
        for record in records:
            label = gold_labels.get(int(record["position_id"]))
            if label is not None:
                record.update(
                    {
                        "gold_status": label.expected_status,
                        "gold_position_types": list(label.accepted_position_types),
                        "gold_opportunity_kinds": list(label.accepted_opportunity_kinds),
                    }
                )
    result = {
        "model": model_settings.llm.model,
        "warmup": warmup,
        "summary": summarize_model(
            records,
            recorder,
            wall_seconds=wall_seconds,
            requested_position_ids=[candidate.position_id for candidate in candidates],
            gold_labels=gold_labels,
        ),
        "requests": [asdict(request) for request in recorder.requests],
        "terminal_failures": recorder.terminal_failures,
        "decisions": records,
    }
    if run_error:
        result["error"] = run_error
    return result


async def warm_up_model(settings: Settings, candidate: Candidate) -> dict[str, Any]:
    """Load and exercise one model; every request here is excluded from scores."""
    recorder = TelemetryRecorder()
    started = monotonic()
    error: str | None = None
    try:
        with _record_production_calls(recorder):
            await auto_review._resilient_ollama_review(settings, [candidate.review_row])
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        error = f"{type(exc).__name__}: {str(exc)[:1_500]}"
    result: dict[str, Any] = {
        "excluded_from_metrics": True,
        "position_id": candidate.position_id,
        "wall_seconds": monotonic() - started,
        "requests": len(recorder.requests),
        "prompt_tokens": sum(request.prompt_tokens for request in recorder.requests),
        "completion_tokens": sum(request.completion_tokens for request in recorder.requests),
        "ollama_load_duration_seconds": sum(
            request.load_duration_seconds for request in recorder.requests
        ),
        "terminal_native_failures": len(recorder.terminal_failures),
    }
    if error:
        result["error"] = error
    return result


def summarize_model(
    decisions: Sequence[dict[str, Any]],
    recorder: TelemetryRecorder,
    *,
    wall_seconds: float,
    requested_position_ids: Sequence[int] | None = None,
    gold_labels: dict[int, GoldLabel] | None = None,
) -> dict[str, Any]:
    requests = recorder.requests
    latencies = sorted(
        float(item["latency_seconds"])
        for item in decisions
        if isinstance(item.get("latency_seconds"), (int, float))
    )
    returned_ids = [int(item["position_id"]) for item in decisions]
    if len(set(returned_ids)) != len(returned_ids):
        raise ValueError("returned decisions contain duplicate position IDs")
    requested_ids = (
        returned_ids
        if requested_position_ids is None
        else list(dict.fromkeys(requested_position_ids))
    )
    unexpected = sorted(set(returned_ids) - set(requested_ids))
    if unexpected:
        raise ValueError(f"returned decisions contain unrequested position IDs: {unexpected}")
    missing_ids = [position_id for position_id in requested_ids if position_id not in set(returned_ids)]
    requested = len(requested_ids)
    returned = len(returned_ids)
    missing = len(missing_ids)
    valid = sum(bool(item["contract_valid"]) for item in decisions)
    manual = [item for item in decisions if item["reference_is_manual"]]
    request_exposures = [recorder.request_exposures.get(position_id, 0) for position_id in requested_ids]
    summary = {
        "candidates": requested,
        "requested_candidates": requested,
        "returned_decisions": returned,
        "missing_decisions": missing,
        "missing_position_ids": missing_ids,
        "completion_rate": _ratio(returned, requested),
        "contract_valid": valid,
        "contract_valid_rate": _ratio(valid, requested),
        "returned_contract_valid_rate": _ratio(valid, returned),
        "fallback_decisions": returned - valid,
        "technical_failures": missing + returned - valid,
        "decision_counts": dict(Counter(str(item["decision"]) for item in decisions)),
        "routed_status_counts": dict(Counter(str(item["routed_status"]) for item in decisions)),
        "reference_status_agreement": _agreement(decisions, "routed_status", "reference_status"),
        "manual_reference_agreement": _agreement(manual, "routed_status", "reference_status"),
        "wall_seconds": wall_seconds,
        "decision_latency_p50_seconds": _percentile(latencies, 0.50),
        "decision_latency_p95_seconds": _percentile(latencies, 0.95),
        "decisions_requiring_retry": sum(exposures > 1 for exposures in request_exposures),
        "mean_request_exposures": (
            sum(request_exposures) / len(request_exposures)
            if request_exposures
            else None
        ),
        "requests": len(requests),
        "http_failures": sum(
            request.transport_error is not None
            or (request.status_code is not None and request.status_code >= 400)
            for request in requests
        ),
        "missing_tool_calls": sum(request.missing_tool_call for request in requests),
        "malformed_json_calls": sum(request.malformed_json for request in requests),
        "malformed_tool_shapes": sum(request.malformed_tool_shape for request in requests),
        "validation_feedback_requests": sum(request.validation_feedback is not None for request in requests),
        "terminal_native_failures": len(recorder.terminal_failures),
        "prompt_tokens": sum(request.prompt_tokens for request in requests),
        "completion_tokens": sum(request.completion_tokens for request in requests),
        "ollama_total_duration_seconds": sum(request.total_duration_seconds for request in requests),
        "ollama_load_duration_seconds": sum(request.load_duration_seconds for request in requests),
        "ollama_prompt_eval_seconds": sum(request.prompt_eval_duration_seconds for request in requests),
        "ollama_generation_seconds": sum(request.eval_duration_seconds for request in requests),
    }
    if gold_labels:
        summary["gold"] = score_gold(decisions, gold_labels)
    return summary


def score_gold(
    decisions: Sequence[dict[str, Any]],
    labels: dict[int, GoldLabel],
) -> dict[str, Any]:
    """Score a selective classifier without pretending an abstention is correct."""
    by_id = {int(item["position_id"]): item for item in decisions}
    correct = 0
    incorrect = 0
    abstentions = 0
    technical_abstentions = 0
    false_acceptances = 0
    false_rejections = 0
    type_correct = 0
    type_scored = 0
    kind_correct = 0
    kind_scored = 0
    joint_correct = 0
    outcomes: list[dict[str, Any]] = []

    for position_id, label in labels.items():
        decision = by_id.get(position_id)
        actual = None if decision is None else str(decision["routed_status"])
        contract_valid = bool(decision and decision.get("contract_valid"))
        if decision is None or not contract_valid:
            disposition = "technical_abstention"
            technical_abstentions += 1
            abstentions += 1
        elif actual == "review":
            disposition = "abstention"
            abstentions += 1
        elif actual == label.expected_status:
            disposition = "correct"
            correct += 1
        else:
            disposition = "incorrect"
            incorrect += 1
            if actual == "eligible" and label.expected_status == "rejected":
                false_acceptances += 1
            elif actual == "rejected" and label.expected_status == "eligible":
                false_rejections += 1

        type_match: bool | None = None
        kind_match: bool | None = None
        if (
            label.expected_status == "eligible"
            and actual == "eligible"
            and decision is not None
            and contract_valid
        ):
            type_scored += 1
            kind_scored += 1
            type_match = str(decision["position_type"]) in label.accepted_position_types
            kind_match = str(decision["opportunity_kind"]) in label.accepted_opportunity_kinds
            type_correct += int(type_match)
            kind_correct += int(kind_match)
        status_match = disposition == "correct"
        joint_match = status_match and (
            label.expected_status == "rejected" or (type_match is True and kind_match is True)
        )
        joint_correct += int(joint_match)
        outcomes.append(
            {
                "position_id": position_id,
                "expected_status": label.expected_status,
                "actual_status": actual,
                "disposition": disposition,
                "position_type_match": type_match,
                "opportunity_kind_match": kind_match,
                "joint_match": joint_match,
            }
        )

    total = len(labels)
    resolved = correct + incorrect
    return {
        "candidates": total,
        "correct": correct,
        "incorrect": incorrect,
        "abstentions": abstentions,
        "technical_abstentions": technical_abstentions,
        "coverage": _ratio(resolved, total),
        "accuracy": _ratio(correct, total),
        "selective_accuracy": _ratio(correct, resolved),
        "abstention_rate": _ratio(abstentions, total),
        "false_acceptances": false_acceptances,
        "false_rejections": false_rejections,
        "eligible_position_type_accuracy": _ratio(type_correct, type_scored),
        "eligible_opportunity_kind_accuracy": _ratio(kind_correct, kind_scored),
        "joint_accuracy": _ratio(joint_correct, total),
        "outcomes": outcomes,
    }


def compare_models(model_results: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for left_index, left in enumerate(model_results):
        left_by_id = {item["position_id"]: item for item in left["decisions"]}
        for right in model_results[left_index + 1 :]:
            right_by_id = {item["position_id"]: item for item in right["decisions"]}
            shared_ids = sorted(left_by_id.keys() & right_by_id.keys())
            pairs = [(left_by_id[position_id], right_by_id[position_id]) for position_id in shared_ids]
            disagreements = [
                {
                    "position_id": left_item["position_id"],
                    "title": left_item["title"],
                    "left": {
                        key: left_item[key]
                        for key in ("decision", "routed_status", "position_type", "opportunity_kind", "confidence")
                    },
                    "right": {
                        key: right_item[key]
                        for key in ("decision", "routed_status", "position_type", "opportunity_kind", "confidence")
                    },
                }
                for left_item, right_item in pairs
                if left_item["routed_status"] != right_item["routed_status"]
            ]
            comparisons.append(
                {
                    "left": left["model"],
                    "right": right["model"],
                    "shared_candidates": len(pairs),
                    "raw_decision_agreement": _pair_agreement(pairs, "decision"),
                    "routed_status_agreement": _pair_agreement(pairs, "routed_status"),
                    "position_type_agreement": _pair_agreement(pairs, "position_type"),
                    "opportunity_kind_agreement": _pair_agreement(pairs, "opportunity_kind"),
                    "disagreements": disagreements,
                }
            )
    return comparisons


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _agreement(rows: Sequence[dict[str, Any]], left: str, right: str) -> float | None:
    return _ratio(sum(item[left] == item[right] for item in rows), len(rows))


def _pair_agreement(pairs: Sequence[tuple[dict[str, Any], dict[str, Any]]], field_name: str) -> float | None:
    return _ratio(sum(left[field_name] == right[field_name] for left, right in pairs), len(pairs))


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    index = min(math.ceil(percentile * len(values)) - 1, len(values) - 1)
    return values[max(index, 0)]


def build_report(
    *,
    candidates: Sequence[Candidate],
    results: Sequence[dict[str, Any]],
    cohort: str,
    seed: int,
    batch_size: int,
    gold_set: GoldSet | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "benchmark": "phdbot-production-auto-review-contract-v1",
        "read_only": True,
        "notes": [
            "Stored PHDBOT statuses are drift references, not a labelled gold standard.",
            "Gold accuracy counts abstentions as unresolved; selective accuracy scores only non-abstaining verdicts.",
            "Contract-valid means the decision survived the production tool, schema, ID, evidence and routing validators.",
            "Fallback decisions are conservative review outputs after the production retry/split budget was exhausted.",
            "Retry metrics count every model request that still asked for a candidate, including resilient batch splits.",
            "Warm-up calls exercise model loading and tools once per model and are excluded from every metric.",
        ],
        "sample": {
            "cohort": cohort,
            "seed": seed,
            "batch_size": batch_size,
            "position_ids": [candidate.position_id for candidate in candidates],
            "strata": dict(Counter(candidate.stratum for candidate in candidates)),
        },
        "models": list(results),
        "comparisons": compare_models(results),
    }
    if gold_set is not None:
        report["gold_set"] = {
            "schema_version": gold_set.schema_version,
            "name": gold_set.name,
            "version": gold_set.version,
            "labelled_at": gold_set.labelled_at,
            "valid_until": gold_set.valid_until.isoformat() if gold_set.valid_until else None,
            "source": str(gold_set.source_path),
            "position_ids": [label.position_id for label in gold_set.labels],
        }
        report["sample"]["cohort"] = "gold"
    return report


def render_markdown(report: dict[str, Any]) -> str:
    has_gold = "gold_set" in report
    lines = [
        "# PHDBOT automatic-review A/B benchmark",
        "",
        f"Fixed sample: `{', '.join(str(value) for value in report['sample']['position_ids'])}`",
        "",
        (
            f"Gold set: `{report['gold_set']['version']}`."
            if has_gold
            else "Stored decisions are a drift reference, not a gold standard."
        ),
        "",
        (
            "| Model | Gold accuracy | Joint accuracy | Coverage | Selective accuracy | Abstention | Valid | Requests | Wall |"
            if has_gold
            else "| Model | Valid | Fallback | Requests | Feedback | Missing tool | Prompt tok | Output tok | Wall |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model in report["models"]:
        summary = model["summary"]
        if has_gold:
            gold = summary["gold"]
            lines.append(
                f"| {model['model']} | {_format_percent(gold['accuracy'])} | "
                f"{_format_percent(gold['joint_accuracy'])} | "
                f"{_format_percent(gold['coverage'])} | {_format_percent(gold['selective_accuracy'])} | "
                f"{_format_percent(gold['abstention_rate'])} | "
                f"{summary['contract_valid']}/{summary['candidates']} | {summary['requests']} | "
                f"{summary['wall_seconds']:.2f}s |"
            )
        else:
            lines.append(
                f"| {model['model']} | {summary['contract_valid']}/{summary['candidates']} | "
                f"{summary['fallback_decisions']} | {summary['requests']} | "
                f"{summary['validation_feedback_requests']} | {summary['missing_tool_calls']} | "
                f"{summary['prompt_tokens']} | {summary['completion_tokens']} | "
                f"{summary['wall_seconds']:.2f}s |"
            )
        if model.get("error"):
            lines.append(f"\nModel error: `{model['error']}`\n")
    for comparison in report["comparisons"]:
        lines.extend(
            (
                "",
                f"## {comparison['left']} vs {comparison['right']}",
                "",
                f"- Routed status agreement: {_format_percent(comparison['routed_status_agreement'])}",
                f"- Raw decision agreement: {_format_percent(comparison['raw_decision_agreement'])}",
                f"- Position type agreement: {_format_percent(comparison['position_type_agreement'])}",
                f"- Opportunity kind agreement: {_format_percent(comparison['opportunity_kind_agreement'])}",
                f"- Routed disagreements: {len(comparison['disagreements'])}",
            )
        )
    return "\n".join(lines) + "\n"


def _format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--alternate-model",
        action="append",
        default=[],
        help="alternate local Ollama model; repeatable; configured model is always the baseline",
    )
    parser.add_argument("--id", type=_positive_int, action="append", default=[], help="fixed position ID; repeatable")
    parser.add_argument("--sample-size", type=_positive_int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--batch-size", type=_positive_int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--cohort", choices=("review", "manual", "all"), default="review")
    parser.add_argument(
        "--gold-set",
        type=Path,
        help=(
            "versioned human-labelled JSON set; use "
            f"{DEFAULT_GOLD_SET.relative_to(DEFAULT_GOLD_SET.parents[1])} for the bundled set"
        ),
    )
    parser.add_argument("--output", type=Path, help="write the complete JSON report here")
    parser.add_argument("--markdown-output", type=Path, help="write a concise Markdown report here")
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv()
    settings = Settings()
    models = list(
        dict.fromkeys(
            [
                normalize_ollama_model(settings.llm.model),
                *(normalize_ollama_model(model) for model in args.alternate_model),
            ]
        )
    )
    if len(models) < 2:
        raise ValueError("provide at least one --alternate-model for an A/B comparison")
    if not settings.llm.api_base:
        raise ValueError("the configured local Ollama api_base is required")
    gold_set = load_gold_set(args.gold_set) if args.gold_set else None
    if gold_set is not None and args.id:
        raise ValueError("--gold-set and --id are mutually exclusive")
    selected_ids = [label.position_id for label in gold_set.labels] if gold_set else args.id
    candidates = await load_candidates(
        settings,
        ids=selected_ids,
        cohort=args.cohort,
        sample_size=args.sample_size,
        seed=args.seed,
    )
    if not candidates:
        raise ValueError("the selected cohort contains no model-reviewable candidates")
    if gold_set is not None:
        validate_gold_candidates(candidates, gold_set)
    gold_labels = gold_set.by_id if gold_set else None
    results = [
        await evaluate_model(
            settings,
            model,
            candidates,
            batch_size=args.batch_size,
            gold_labels=gold_labels,
        )
        for model in models
    ]
    return build_report(
        candidates=candidates,
        results=results,
        cohort=args.cohort,
        seed=args.seed,
        batch_size=args.batch_size,
        gold_set=gold_set,
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    report = asyncio.run(async_main(args))
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    if not args.output:
        print(payload)
    else:
        print(render_markdown(report))


if __name__ == "__main__":
    main()
