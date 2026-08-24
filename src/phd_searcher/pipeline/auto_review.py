"""Automatic, auditable review of candidates left ambiguous by deterministic rules."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal

import httpx
from injector import Injector
from litellm import acompletion
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.clock import local_today
from phd_searcher.config import Settings
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.opportunity_kinds import OPPORTUNITY_KINDS, OpportunityKind
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.review_audit import append_review_attempt
from phd_searcher.pipeline.review_context import (
    evidence_quote_present,
    opportunity_kind_evidence_supports,
    triage_evidence_supports,
)
from phd_searcher.position_types import POSITION_TYPES, classify_position
from phd_searcher.screening import screen_position

REVIEW_VERSION = "hybrid-v5"
_COMPATIBLE_REVIEW_VERSIONS = ("hybrid-v3", "hybrid-v4", REVIEW_VERSION)
_BATCH_SIZE = 8
_ELIGIBLE_THRESHOLD = 0.90
_REJECT_THRESHOLD = 0.97
_MAX_DESCRIPTION = 4_000
_MIN_ATTRIBUTABLE_TEXT_CHARS = 200
_ELIGIBLE_OPPORTUNITY_KINDS = frozenset({"vacancy", "programme", "spontaneous"})
_SPACE = re.compile(r"\s+")
_TYPE_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:phd|doctoral|doctorate)\b", re.I), "phd"),
    (re.compile(r"\b(?:master|mph|mphil|thesis)\b", re.I), "masters_mph"),
    (re.compile(r"\b(?:medical|md.?phd|doctor of medicine)\b", re.I), "medical_doctorate"),
    (re.compile(r"\b(?:intern|internship|trainee|traineeship|tirocinio|praktikum)\b", re.I), "internship"),
    (re.compile(r"\b(?:assistant|assistantship|teaching assistant|research assistant)\b", re.I), "assistantship"),
    (re.compile(r"\b(?:fellow|fellowship|grant)\b", re.I), "research_fellowship"),
    (re.compile(r"\bpost.?doc", re.I), "postdoc"),
    (re.compile(r"\b(?:research staff|researcher|research scientist|research engineer)\b", re.I), "research_staff"),
    (re.compile(r"\b(?:faculty|lecturer|professor)\b", re.I), "faculty"),
    (
        re.compile(
            r"\b(?:non[- ]vacancy|non[- ]opportunity|not (?:a )?vacancy|"
            r"irrelevant|not applicable|vacancy|job|position|opportunity|"
            r"course|news|event|information page)\b|^n/?a$",
            re.I,
        ),
        "other",
    ),
    (re.compile(r"\bother\b", re.I), "other"),
)

_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_position_reviews",
        "description": (
            "Submit one independent review for every requested position. position_id is an idempotency "
            "key: valid reviews are retained while the application asks again only for missing or invalid IDs."
        ),
        "parameters": {
            "type": "object",
            "required": ["reviews"],
            "properties": {
                "reviews": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": [
                            "position_id",
                            "decision",
                            "position_type",
                            "opportunity_kind",
                            "confidence",
                            "evidence",
                        ],
                        "properties": {
                            "position_id": {"type": "integer"},
                            "decision": {
                                "type": "string",
                                "enum": ["eligible", "rejected", "review"],
                            },
                            "position_type": {
                                "type": "string",
                                "enum": list(POSITION_TYPES),
                            },
                            "opportunity_kind": {
                                "type": "string",
                                "enum": sorted(OPPORTUNITY_KINDS),
                            },
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "evidence": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 10, "maxLength": 500},
                                "minItems": 1,
                                "maxItems": 4,
                            },
                        },
                        "additionalProperties": False,
                    },
                }
            },
            "additionalProperties": False,
        },
    },
}


class AutomaticDecision(BaseModel):
    position_id: int
    decision: Literal["eligible", "rejected", "review"]
    position_type: str
    opportunity_kind: OpportunityKind
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=220)
    evidence: list[str] = Field(min_length=1, max_length=4)
    tool_attempts: int = Field(default=1, ge=1, exclude=True)
    latency_seconds: float | None = Field(default=None, ge=0, exclude=True)


class AutomaticDecisionBatch(BaseModel):
    reviews: list[AutomaticDecision]


@dataclass(frozen=True, slots=True)
class _PartialToolResult:
    decisions: tuple[AutomaticDecision, ...]
    errors: tuple[str, ...]


class _IncompleteAutomaticReviewError(RuntimeError):
    """Keep valid per-position decisions available when a batch remains incomplete."""

    def __init__(self, message: str, decisions: list[AutomaticDecision]) -> None:
        super().__init__(message)
        self.decisions = tuple(decisions)


def _compact(value: str, limit: int = _MAX_DESCRIPTION) -> str:
    return _SPACE.sub(" ", value).strip()[:limit]


def _normalized_text_length(value: str) -> int:
    """Match the whitespace normalization used by inline evidence routing."""
    return len(" ".join(value.split()))


def _accepted_status(decision: AutomaticDecision) -> Literal["eligible", "rejected", "review"]:
    if (
        decision.decision == "eligible"
        and decision.confidence >= _ELIGIBLE_THRESHOLD
        and _eligible_kind_is_coherent(decision)
    ):
        return "eligible"
    if decision.decision == "rejected" and decision.confidence >= _REJECT_THRESHOLD:
        return "rejected"
    return "review"


def _eligible_kind_is_coherent(decision: AutomaticDecision) -> bool:
    """An actionable kind must be proven by the same attributable evidence."""
    return (
        decision.opportunity_kind in _ELIGIBLE_OPPORTUNITY_KINDS
        and opportunity_kind_evidence_supports(
            decision.evidence,
            decision.opportunity_kind,
        )
    )


def _deterministic_opportunity_kind(status: str, reason: str) -> OpportunityKind:
    """Only deterministic positive vacancy rules establish a concrete kind."""
    if status == "eligible" and (
        reason == "vacancy_signal" or reason.startswith("recognized_type:")
    ):
        return "vacancy"
    return "unknown"


def _routed_status(
    decision: AutomaticDecision,
    *,
    has_full_description: bool,
) -> tuple[Literal["eligible", "rejected", "review"], bool]:
    """Un rifiuto LLM richiede la pagina dettaglio, non il solo snippet."""
    accepted = _accepted_status(decision)
    needs_detail = (
        decision.decision == "rejected"
        and accepted == "rejected"
        and not has_full_description
    )
    return ("review" if needs_detail else accepted, needs_detail)


def _review_state(
    accepted_status: Literal["eligible", "rejected", "review"],
    available_text: str,
) -> Literal["resolved", "needs_evidence", "semantic_uncertain"]:
    if accepted_status != "review":
        return "resolved"
    normalized_length = _normalized_text_length(available_text)
    return (
        "needs_evidence"
        if normalized_length < _MIN_ATTRIBUTABLE_TEXT_CHARS
        else "semantic_uncertain"
    )


def _attributable_text(position: Position) -> str:
    """Return only listing text that can be attributed to this candidate."""
    return position.description or position.title


def _needs_evidence_bypass(position: Position) -> bool:
    """Skip semantic review when the listing cannot support a safe verdict."""
    if position.full_description is not None:
        return False
    normalized_length = _normalized_text_length(_attributable_text(position))
    return normalized_length < _MIN_ATTRIBUTABLE_TEXT_CHARS


def _canonical_position_type(value: str, *, unknown_as_other: bool = False) -> str:
    normalized = value.strip().casefold()
    for key, label in POSITION_TYPES.items():
        if normalized in (key.casefold(), label.casefold()):
            return key
    for pattern, key in _TYPE_ALIASES:
        if pattern.search(value):
            return key
    if unknown_as_other:
        return "other"
    raise ValueError(
        f"unknown position type {value!r}; allowed canonical values are {sorted(POSITION_TYPES)}"
    )


def _canonical_decision(value: object) -> str:
    normalized = str(value).strip().casefold().replace("_", " ").replace("-", " ")
    aliases = {
        "eligible": {"eligible", "accept", "accepted", "include", "included", "relevant"},
        "rejected": {"rejected", "reject", "ineligible", "exclude", "excluded", "irrelevant"},
        "review": {"review", "manual review", "uncertain", "unclear", "unknown", "ambiguous"},
    }
    for canonical, variants in aliases.items():
        if normalized in variants:
            return canonical
    raise ValueError(f"unknown decision {value!r}; allowed values are eligible, rejected, review")


def _adopt_alias(item: dict[str, object], target: str, aliases: tuple[str, ...]) -> None:
    if item.get(target) is not None:
        return
    for alias in aliases:
        if item.get(alias) is not None:
            item[target] = item[alias]
            return


def _raw_tool_reviews(call: object) -> list[object]:
    if isinstance(call, dict):
        function = call.get("function")
        if not isinstance(function, dict):
            raise ValueError("tool call has no function object")
        raw = function.get("arguments")
    else:
        function = getattr(call, "function", None)
        raw = getattr(function, "arguments", None)
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(payload, dict):
        raise ValueError("tool arguments must be a JSON object")
    reviews = payload.get("reviews")
    if not isinstance(reviews, list):
        raise ValueError("tool arguments must contain a reviews array")
    return reviews


def _prepared_tool_items(
    raw_reviews: list[object],
    expected_order: list[int],
    *,
    repair_missing_fields: bool,
) -> list[object]:
    prepared: list[object] = []
    for raw_item in raw_reviews:
        if not isinstance(raw_item, dict):
            prepared.append(raw_item)
            continue
        item = dict(raw_item)
        if repair_missing_fields:
            _adopt_alias(item, "position_id", ("id", "candidate_id"))
            _adopt_alias(item, "decision", ("status", "verdict", "classification", "decision_type", "eligibility"))
            _adopt_alias(item, "position_type", ("type", "category", "opportunity_type"))
            _adopt_alias(item, "opportunity_kind", ("kind", "opportunity_scope", "page_kind"))
            _adopt_alias(item, "reason", ("rationale", "explanation", "justification"))
            _adopt_alias(item, "evidence", ("quote", "quotes", "evidence_quotes", "supporting_evidence"))
            if item.get("decision") is not None:
                with suppress(ValueError):
                    item["decision"] = _canonical_decision(item["decision"])
            if isinstance(item.get("evidence"), str):
                item["evidence"] = [item["evidence"]]
            item.setdefault("position_type", "other")
        prepared.append(item)

    objects = [item for item in prepared if isinstance(item, dict)]
    supplied_ids = [item.get("position_id") for item in objects]
    # The final compatibility repair may infer IDs by order only when no ID is
    # present at all and there is an unambiguous one-to-one correspondence.
    if (
        repair_missing_fields
        and objects
        and len(objects) == len(prepared) == len(expected_order)
        and not any(value is not None for value in supplied_ids)
    ):
        for index, item in enumerate(objects):
            item["position_id"] = expected_order[index]
    return prepared


def _id_hint(item: object) -> int | None:
    if not isinstance(item, dict):
        return None
    value = item.get("position_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _decision_from_item(
    item: object,
    *,
    unknown_types_as_other: bool,
) -> AutomaticDecision:
    if not isinstance(item, dict):
        raise ValueError("review must be an object")
    normalized = dict(item)
    if not str(normalized.get("reason") or "").strip():
        evidence = normalized.get("evidence")
        fragment = (
            str(evidence[0])
            if isinstance(evidence, list) and evidence and str(evidence[0]).strip()
            else "no concise evidence supplied"
        )
        normalized["reason"] = f"{normalized.get('decision', 'review')}: {fragment}"[:220]
    decision = AutomaticDecision.model_validate(normalized)
    return decision.model_copy(
        update={
            "position_type": _canonical_position_type(
                decision.position_type,
                unknown_as_other=unknown_types_as_other,
            )
        }
    )


def _validation_summary(exc: ValidationError) -> str:
    details = []
    for error in exc.errors()[:6]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "review"
        details.append(f"{location}: {error.get('msg', 'invalid value')}")
    return "; ".join(details)


def _parse_tool_call_partial(
    call: object,
    expected_ids: set[int] | list[int],
    *,
    accepted_ids: set[int] | None = None,
    unknown_types_as_other: bool = False,
    repair_missing_fields: bool = False,
    evidence_contexts: dict[int, str] | None = None,
) -> _PartialToolResult:
    """Validate each review independently and retain every valid expected ID."""
    expected_order = sorted(expected_ids) if isinstance(expected_ids, set) else list(expected_ids)
    expected_set = set(expected_order)
    already_accepted = accepted_ids or set()
    raw_reviews = _raw_tool_reviews(call)
    prepared = _prepared_tool_items(
        raw_reviews,
        expected_order,
        repair_missing_fields=repair_missing_fields,
    )
    decisions: dict[int, AutomaticDecision] = {}
    errors: list[str] = []
    for index, item in enumerate(prepared):
        hint = _id_hint(item)
        # Replaying an already accepted position is harmless: position_id is
        # the idempotency key, and its first valid decision remains immutable.
        if hint in already_accepted:
            continue
        try:
            decision = _decision_from_item(
                item,
                unknown_types_as_other=unknown_types_as_other,
            )
        except ValidationError as exc:
            label = f"position_id={hint}" if hint is not None else f"reviews[{index}]"
            errors.append(f"{label}: {_validation_summary(exc)}")
            continue
        except (TypeError, ValueError) as exc:
            label = f"position_id={hint}" if hint is not None else f"reviews[{index}]"
            errors.append(f"{label}: {str(exc)[:300]}")
            continue
        if decision.position_id in already_accepted or decision.position_id in decisions:
            continue
        if decision.position_id not in expected_set:
            errors.append(f"unexpected position_id={decision.position_id}")
            continue
        if evidence_contexts is not None:
            context = evidence_contexts.get(decision.position_id, "")
            invalid_quotes = [
                quote for quote in decision.evidence if not evidence_quote_present(quote, context)
            ]
            if invalid_quotes:
                errors.append(
                    f"position_id={decision.position_id}: evidence is not verbatim: "
                    f"{invalid_quotes[:2]}"
                )
                continue
            if decision.decision == "eligible" and not _eligible_kind_is_coherent(decision):
                errors.append(
                    f"position_id={decision.position_id}: evidence does not support actionable "
                    f"opportunity_kind={decision.opportunity_kind}; eligible requires a grounded "
                    "vacancy, programme or spontaneous application route"
                )
                continue
            if not triage_evidence_supports(
                decision.evidence,
                decision=decision.decision,
                position_type=decision.position_type,
            ):
                errors.append(
                    f"position_id={decision.position_id}: evidence does not support "
                    f"the {decision.decision} decision"
                )
                continue
        decisions[decision.position_id] = decision

    missing = [position_id for position_id in expected_order if position_id not in decisions]
    if missing:
        errors.append(f"missing position IDs: {missing}")
    return _PartialToolResult(tuple(decisions.values()), tuple(errors))


def _parse_tool_call(
    call: object,
    expected_ids: set[int] | list[int],
    *,
    unknown_types_as_other: bool = False,
    repair_missing_fields: bool = False,
) -> list[AutomaticDecision]:
    """Strict compatibility wrapper used when a complete batch is required."""
    expected_order = sorted(expected_ids) if isinstance(expected_ids, set) else list(expected_ids)
    parsed = _parse_tool_call_partial(
        call,
        expected_order,
        unknown_types_as_other=unknown_types_as_other,
        repair_missing_fields=repair_missing_fields,
    )
    if parsed.errors:
        raise ValueError("; ".join(parsed.errors))
    by_id = {decision.position_id: decision for decision in parsed.decisions}
    return [by_id[position_id] for position_id in expected_order]


def _prompt(rows: list[tuple[Position, University | None]]) -> str:
    candidates = [
        {
            "position_id": position.id,
            "title": position.title,
            "url": position.url,
            "university": university.name if university else position.institution_name,
            "country": university.country if university else position.institution_country,
            "current_type": position.position_type,
            "description": _compact(position.full_description or position.description),
        }
        for position, university in rows
    ]
    return (
        "Review the following web-extracted candidates for an academic opportunity search. "
        "Eligible means an actual open PhD/doctorate, Master/MPH, medical doctorate, internship/traineeship, "
        "assistantship, research fellowship/grant, postdoc, research staff or faculty opportunity. Reject only when "
        "the supplied evidence clearly shows a course/degree information page, navigation, news/event, "
        "closed/unavailable call, generic funding information, or another non-vacancy page. Pay special "
        "attention to negation and do not interpret identifiers such as 'No. 1 position' as absence. "
        "Classify opportunity_kind independently: vacancy is a specific advertised role, call or project; "
        "programme is a named degree or graduate programme with an explicitly current or future intake; "
        "spontaneous is an explicit unsolicited/speculative application or expression-of-interest route without "
        "an advertised slot; information is an evergreen procedural, news or FAQ page; unknown means the supplied "
        "text cannot establish a kind. Eligible is allowed only for vacancy, programme or spontaneous when the "
        "quoted evidence supports that exact kind. A generic 'How to apply' page proves a procedure, not an open "
        "vacancy or intake. "
        "If evidence is incomplete, mixed, or merely suggestive, choose review. Confidence is confidence "
        "in that exact decision, not general topical similarity. Evidence must quote short fragments from "
        "the supplied candidate. Call submit_position_reviews exactly once and include every ID. Supply only "
        "position_id, decision, position_type, opportunity_kind, confidence and evidence for each review.\n\n"
        f"{json.dumps(candidates, ensure_ascii=False)}"
    )


async def _native_ollama_review(
    settings: Settings,
    rows: list[tuple[Position, University | None]],
    *,
    max_attempts: int = 3,
) -> list[AutomaticDecision]:
    expected_order = [position.id for position, _ in rows]
    prompt = _prompt(rows)
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    accepted: dict[int, AutomaticDecision] = {}
    evidence_contexts = {
        position.id: _compact(
            f"{position.title}\n{position.full_description or position.description}"
        )
        for position, _university in rows
    }
    base_url = (settings.llm.api_base or "").rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    last_error = "the model did not call the review tool"
    started = monotonic()

    for _attempt in range(max_attempts):
        native_ollama = settings.llm.model.startswith("ollama/") and bool(base_url)
        if native_ollama:
            payload = {
                "model": settings.llm.model.removeprefix("ollama/"),
                "messages": messages,
                "tools": [_REVIEW_TOOL],
                "stream": False,
                "think": "low",
                "options": {"temperature": 0, "num_ctx": 32768},
            }
            async with httpx.AsyncClient(timeout=600) as client:
                try:
                    response = await client.post(f"{base_url}/api/chat", json=payload)
                except httpx.RequestError as exc:
                    last_error = f"Ollama transport error: {type(exc).__name__}: {str(exc)[:300]}"
                    await asyncio.sleep(min(2 ** (_attempt + 1), 8))
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    try:
                        detail = str(response.json().get("error") or response.text)
                    except (TypeError, ValueError):
                        detail = response.text
                    last_error = f"Ollama HTTP {response.status_code}: {detail[:500]}"
                    await asyncio.sleep(min(2 ** (_attempt + 1), 8))
                    continue
                response.raise_for_status()
            message: dict[str, Any] = response.json().get("message") or {}
            calls = list(message.get("tool_calls") or [])
        else:
            response = await acompletion(
                model=settings.llm.model,
                api_base=settings.llm.api_base,
                api_key=settings.llm.api_key,
                messages=messages,
                tools=[_REVIEW_TOOL],
                tool_choice="required",
                temperature=0,
            )
            raw_message = response.choices[0].message
            message = {
                "role": "assistant",
                "content": raw_message.content or "",
                "tool_calls": [call.model_dump(exclude_none=True) for call in raw_message.tool_calls or []],
            }
            calls = list(raw_message.tool_calls or [])
        messages.append(message)
        if calls:
            try:
                pending_order = [position_id for position_id in expected_order if position_id not in accepted]
                parsed = _parse_tool_call_partial(
                    calls[0],
                    pending_order,
                    accepted_ids=set(accepted),
                    # Il tool riceve prima due feedback rigorosi. All'ultimo
                    # tentativo unicamente il tipo semantico ignoto degrada a
                    # `other`; JSON, campi minimi, ID e decisioni restano obbligatori.
                    unknown_types_as_other=_attempt == max_attempts - 1,
                    repair_missing_fields=_attempt == max_attempts - 1,
                    evidence_contexts=evidence_contexts,
                )
                accepted.update(
                    (
                        decision.position_id,
                        decision.model_copy(
                            update={
                                "tool_attempts": _attempt + 1,
                                "latency_seconds": monotonic() - started,
                            }
                        ),
                    )
                    for decision in parsed.decisions
                )
                pending_order = [position_id for position_id in expected_order if position_id not in accepted]
                if not pending_order:
                    return [accepted[position_id] for position_id in expected_order]
                last_error = "; ".join(parsed.errors)[:1_200]
            except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
                if isinstance(exc, ValidationError):
                    fields = sorted(
                        {
                            str(error["loc"][-1])
                            for error in exc.errors()
                            if error.get("loc")
                        }
                    )
                    last_error = (
                        "Invalid tool arguments: every review must include position_id, decision, "
                        "position_type, opportunity_kind, confidence and evidence. "
                        f"Missing or invalid fields: {', '.join(fields)}"
                    )
                else:
                    last_error = f"Invalid tool arguments: {str(exc)[:800]}"
        else:
            last_error = "No tool call was produced."
        pending_order = [position_id for position_id in expected_order if position_id not in accepted]
        feedback = json.dumps(
            {
                "ok": False,
                "error": "invalid_or_incomplete_reviews",
                "accepted_ids": sorted(accepted),
                "remaining_ids": pending_order,
                "details": last_error,
                "instruction": (
                    "Valid IDs are retained. Do not repeat accepted_ids. Call the tool again with exactly "
                    "one valid review for each remaining_id."
                ),
            },
            ensure_ascii=False,
        )
        if not calls:
            messages.append({"role": "user", "content": feedback})
        elif native_ollama:
            messages.append({"role": "tool", "tool_name": "submit_position_reviews", "content": feedback})
        else:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": calls[0].id,
                    "name": "submit_position_reviews",
                    "content": feedback,
                }
            )
    ordered_accepted = [accepted[position_id] for position_id in expected_order if position_id in accepted]
    raise _IncompleteAutomaticReviewError(
        f"automatic review failed after {max_attempts} attempts: {last_error}",
        ordered_accepted,
    )


async def _resilient_ollama_review(
    settings: Settings,
    rows: list[tuple[Position, University | None]],
) -> list[AutomaticDecision]:
    """Isolate malformed model output without blocking the entire review run."""
    try:
        return await _native_ollama_review(settings, rows)
    except RuntimeError as exc:
        accepted = list(exc.decisions) if isinstance(exc, _IncompleteAutomaticReviewError) else []
        accepted_by_id = {decision.position_id: decision for decision in accepted}
        unresolved = [row for row in rows if row[0].id not in accepted_by_id]
        recovered: list[AutomaticDecision]
        if len(unresolved) > 1:
            midpoint = len(unresolved) // 2
            left = await _resilient_ollama_review(settings, unresolved[:midpoint])
            right = await _resilient_ollama_review(settings, unresolved[midpoint:])
            recovered = [*left, *right]
        elif unresolved and accepted:
            # The original multi-item prompt exhausted its feedback budget,
            # but the sole unresolved item still deserves the existing
            # singleton retry before falling back to manual review.
            recovered = await _resilient_ollama_review(settings, unresolved)
        elif unresolved:
            position = unresolved[0][0]
            recovered = [
                AutomaticDecision(
                    position_id=position.id,
                    decision="review",
                    position_type="other",
                    opportunity_kind="unknown",
                    confidence=0.0,
                    reason=f"invalid model tool output: {str(exc)[:180]}",
                    evidence=["automatic review could not produce a valid structured decision"],
                )
            ]
        else:
            recovered = []
        by_id = {decision.position_id: decision for decision in [*accepted, *recovered]}
        return [by_id[position.id] for position, _ in rows]


def _route_to_evidence_without_llm(
    session: AsyncSession,
    position: Position,
    progress: Progress,
    *,
    reviewed_at: datetime,
) -> None:
    """Persist a conservative review result while retaining the evidence cohort."""
    attributable_chars = _normalized_text_length(_attributable_text(position))
    reason = "review1:insufficient_attributable_text"
    position.screening_status = "review"
    position.opportunity_kind = "unknown"
    position.screening_reason = reason
    # Keep deterministic routing distinct from an actual model verdict.  The
    # resume query treats both ``router`` and ``llm`` as completed automatic
    # work when their version is compatible.
    position.screening_source = "router"
    position.screening_decision = "review"
    position.screening_confidence = None
    position.screening_evidence = None
    position.screening_model = None
    position.screening_version = REVIEW_VERSION
    position.screened_at = reviewed_at
    position.review_state = "needs_evidence"
    position.routing_reason = reason
    position.indexed_at = None
    append_review_attempt(
        session,
        position_id=position.id,
        pipeline_run_id=progress.run_id,
        stage="review",
        model=None,
        version=REVIEW_VERSION,
        raw_decision="review",
        accepted_status="review",
        position_type=position.position_type,
        confidence=None,
        evidence=[],
        reason=reason,
        tool_attempts=0,
        details={
            "review_state": "needs_evidence",
            "needs_detail_before_reject": False,
            "llm_bypassed": True,
            "attributable_text_chars": attributable_chars,
            "opportunity_kind": position.opportunity_kind,
        },
    )


async def _apply_review_batch(
    settings: Settings,
    session: AsyncSession,
    batch: list[tuple[Position, University | None]],
    progress: Progress,
) -> None:
    """Review evidence-rich rows and route evidence-poor rows without an LLM call."""
    # The query already excludes manual rows. Keep this guard at the mutation
    # boundary too, so a manual decision can never be overwritten by this stage.
    automatic_batch = [row for row in batch if not row[0].screening_manual]
    bypassed_ids = {
        position.id
        for position, _university in automatic_batch
        if _needs_evidence_bypass(position)
    }
    model_batch = [row for row in automatic_batch if row[0].id not in bypassed_ids]
    decisions = await _resilient_ollama_review(settings, model_batch) if model_batch else []
    by_id = {decision.position_id: decision for decision in decisions}
    reviewed_at = datetime.now(UTC).replace(tzinfo=None)

    for position, _university in automatic_batch:
        if position.id in bypassed_ids:
            _route_to_evidence_without_llm(
                session,
                position,
                progress,
                reviewed_at=reviewed_at,
            )
            continue

        automatic_decision = by_id[position.id]
        accepted_status, needs_detail_before_reject = _routed_status(
            automatic_decision,
            has_full_description=position.full_description is not None,
        )
        position.screening_status = accepted_status
        position.opportunity_kind = automatic_decision.opportunity_kind
        position.position_type = classify_position(
            position.title,
            position.full_description or position.description,
            automatic_decision.position_type,
        )
        position.screening_reason = f"llm:{automatic_decision.reason}"[:256]
        position.screening_source = "llm"
        position.screening_decision = automatic_decision.decision
        position.screening_confidence = automatic_decision.confidence
        position.screening_evidence = json.dumps(automatic_decision.evidence, ensure_ascii=False)
        position.screening_model = settings.llm.model
        position.screening_version = REVIEW_VERSION
        position.screened_at = reviewed_at
        position.review_state = (
            "needs_evidence"
            if needs_detail_before_reject
            else _review_state(
                accepted_status,
                position.full_description or position.description,
            )
        )
        position.routing_reason = (
            "review1:needs_evidence_before_reject"
            if needs_detail_before_reject
            else position.screening_reason
        )
        position.indexed_at = None
        append_review_attempt(
            session,
            position_id=position.id,
            pipeline_run_id=progress.run_id,
            stage="review",
            model=settings.llm.model,
            version=REVIEW_VERSION,
            raw_decision=automatic_decision.decision,
            accepted_status=accepted_status,
            position_type=automatic_decision.position_type,
            confidence=automatic_decision.confidence,
            evidence=automatic_decision.evidence,
            reason=position.screening_reason,
            tool_attempts=automatic_decision.tool_attempts,
            latency_seconds=automatic_decision.latency_seconds,
            details={
                "review_state": position.review_state,
                "needs_detail_before_reject": needs_detail_before_reject,
                "opportunity_kind": automatic_decision.opportunity_kind,
            },
        )


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    """Classify pending candidates with rules, then review ambiguous candidates with an LLM tool."""
    progress = progress or Progress()
    settings = container.get(Settings)
    session_maker = container.get(async_sessionmaker[AsyncSession])
    today = local_today()
    checkpoint = await progress.load_checkpoint()
    raw_reviewed = checkpoint.get("processed", 0)
    reviewed: int = (
        raw_reviewed
        if isinstance(raw_reviewed, int)
        else int(raw_reviewed)
        if isinstance(raw_reviewed, str) and raw_reviewed.isdigit()
        else 0
    )
    remaining = None if limit is None else max(limit - reviewed, 0)

    async with session_maker() as session:
        rule_stmt = select(Position).where(
            Position.is_active.is_(True),
            Position.screening_manual.is_(False),
            or_(
                Position.screening_status == "pending",
                Position.title.ilike("%status:%"),
                Position.title.ilike("%estado:%"),
            ),
        )
        rule_candidates = (await session.execute(rule_stmt)).scalars().all()
        now = datetime.now(UTC).replace(tzinfo=None)
        for position in rule_candidates:
            previous_status = position.screening_status
            rule_decision = screen_position(
                title=position.title,
                url=position.url,
                description=position.full_description or position.description,
                position_type=position.position_type,
            )
            if (
                position.screening_status != "pending"
                and rule_decision.reason != "explicitly_closed_or_unavailable"
            ):
                continue
            position.screening_status = rule_decision.status
            position.opportunity_kind = _deterministic_opportunity_kind(
                rule_decision.status,
                rule_decision.reason,
            )
            position.screening_reason = rule_decision.reason
            position.screening_source = "rules"
            position.screening_decision = rule_decision.status
            position.screening_confidence = 1.0 if rule_decision.status != "review" else None
            deterministic_evidence = (
                [position.title]
                if rule_decision.reason == "explicitly_closed_or_unavailable"
                else []
            )
            position.screening_evidence = (
                json.dumps(deterministic_evidence, ensure_ascii=False)
                if deterministic_evidence
                else None
            )
            position.screening_model = None
            position.screening_version = REVIEW_VERSION
            position.screened_at = now
            if rule_decision.status != "review":
                position.review_state = "resolved"
                position.routing_reason = f"review1:rule:{rule_decision.reason}"
                position.indexed_at = None
            if previous_status != "pending":
                append_review_attempt(
                    session,
                    position_id=position.id,
                    pipeline_run_id=progress.run_id,
                    stage="review",
                    model=None,
                    version=REVIEW_VERSION,
                    raw_decision=rule_decision.status,
                    accepted_status=rule_decision.status,
                    position_type=position.position_type,
                    confidence=position.screening_confidence,
                    evidence=deterministic_evidence,
                    reason=rule_decision.reason,
                    tool_attempts=0,
                    latency_seconds=0,
                    details={
                        "deterministic_override": True,
                        "previous_status": previous_status,
                        "review_state": position.review_state,
                        "opportunity_kind": position.opportunity_kind,
                    },
                )
        await session.commit()

        stmt = (
            select(Position, University)
            .outerjoin(University, Position.university_id == University.id)
            .where(
                Position.is_active.is_(True),
                or_(Position.deadline.is_(None), Position.deadline >= today),
                Position.screening_manual.is_(False),
                Position.screening_status == "review",
                or_(
                    Position.screening_source.not_in(("llm", "router")),
                    Position.screening_version.is_(None),
                    Position.screening_version.not_in(_COMPATIBLE_REVIEW_VERSIONS),
                ),
            )
            .order_by(Position.id)
        )
        if name_like:
            stmt = stmt.where(or_(University.name.ilike(f"%{name_like}%"), Position.institution_name.ilike(f"%{name_like}%")))
        if remaining is not None:
            stmt = stmt.limit(remaining)
        rows: list[tuple[Position, University | None]] = [
            (position, university) for position, university in (await session.execute(stmt)).all()
        ]
        batch_total = (len(rows) + _BATCH_SIZE - 1) // _BATCH_SIZE
        await progress.begin(batch_total)

        for offset in range(0, len(rows), _BATCH_SIZE):
            batch = rows[offset : offset + _BATCH_SIZE]
            await progress.tick(f"automatic review {reviewed + 1}-{reviewed + len(batch)}")
            await _apply_review_batch(settings, session, batch, progress)
            await session.commit()
            reviewed += len(batch)
            await progress.save_checkpoint(
                processed=reviewed,
                last_position_id=batch[-1][0].id,
                cohort_complete=False,
            )
            await progress.check_stop()
            if progress.should_stop:
                break
        if not progress.should_stop:
            await progress.save_checkpoint(cohort_complete=limit is None)
    return reviewed
