"""Seconda review evidence-first, applicata soltanto al residuo della triage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from time import monotonic
from typing import Any, Literal

import httpx
from injector import Injector
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from phd_searcher.clock import local_today
from phd_searcher.config import Settings
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.review_attempt import ReviewAttempt
from phd_searcher.database.models.university import University
from phd_searcher.opportunity_kinds import OPPORTUNITY_KINDS, OpportunityKind
from phd_searcher.pipeline.normalize import extract_deadline
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.review_audit import append_review_attempt
from phd_searcher.pipeline.review_context import (
    application_evidence_supports,
    build_evidence_context,
    classify_opportunity_kind_evidence,
    compact_text,
    evidence_quote_present,
    explicit_negative_evidence_supports,
    negative_evidence_supports,
    opportunity_kind_evidence_supports,
    relative_application_window_is_anchored,
    select_evidence_document,
)
from phd_searcher.pipeline.rule_sweep import apply_rule_sweep
from phd_searcher.position_types import POSITION_TYPES, classify_position
from phd_searcher.screening import screen_position

REVIEW_VERSION = "evidence-v23"
_CACHE_COMPATIBLE_VERSIONS = (
    "evidence-v7",
    "evidence-v8",
    "evidence-v9",
    "evidence-v10",
    "evidence-v11",
    "evidence-v12",
    "evidence-v13",
    "evidence-v14",
    "evidence-v15",
    "evidence-v16",
    "evidence-v17",
    "evidence-v18",
    "evidence-v19",
    "evidence-v20",
    "evidence-v21",
    "evidence-v22",
    REVIEW_VERSION,
)
_REQUEUE_REJECTED_VERSIONS = (
    "hybrid-v2",
    "hybrid-v3",
    "hybrid-v4",
    "evidence-v9",
    "evidence-v10",
    "evidence-v11",
)
_REQUEUE_ELIGIBLE_VERSIONS = (
    "hybrid-v2",
    "hybrid-v3",
    "hybrid-v4",
    "evidence-v5",
    "evidence-v7",
    "evidence-v8",
    "evidence-v9",
    "evidence-v10",
    "evidence-v11",
    "evidence-v12",
    "evidence-v13",
    "evidence-v14",
    "evidence-v15",
    "evidence-v16",
    "evidence-v17",
    "evidence-v18",
)
# Final v15/v17 abstentions remain valid hard residue. v19 requires an explicit
# current/future temporal anchor for every positive open verdict, so earlier
# resolved positives (and the small detail-backed legacy hybrid cohort) need
# another GPU decision. v20 keeps those verdicts compatible while retrying v19
# grounding failures with the expanded localized-date parser. v21 additionally
# repairs exact evidence placed in the wrong tool field and omitted exact
# titles. v22 extends localized deadline/type grounding and admits fully
# validated positives at the model's discrete 0.90 confidence step. v23 pairs
# an omitted exact role title with deterministic type correction in one pass.
_REVIEW_COMPATIBLE_VERSIONS = (
    "evidence-v15",
    "evidence-v17",
    "evidence-v18",
    "evidence-v19",
    "evidence-v20",
    "evidence-v21",
    "evidence-v22",
    REVIEW_VERSION,
)
_THRESHOLD_RETRY_VERSIONS = ("evidence-v20", "evidence-v21")
_RETRYABLE_REVIEW_STATES = ("ready_deep_review", "tool_error")
_ONE_TIME_RETRY_REVIEW_STATES = ("grounding_failure",)
# Review2 is deliberately one dossier at a time.  In production gpt-oss spent
# most of its budget trying to coordinate four independent tool calls, then
# repeated the same work while repairing the missing calls.  A singleton keeps
# the evidence context focused and makes every checkpoint independently durable.
_BATCH_SIZE = 1
_INITIAL_NUM_PREDICT = 2_048
_RETRY_NUM_PREDICT = 2_048
_FINAL_NUM_PREDICT = 6_144
_ELIGIBLE_THRESHOLD = 0.90
_REJECT_THRESHOLD = 0.97
_TYPE_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:medical doctorate|md.?phd|doctor of medicine)\b", re.I), "medical_doctorate"),
    (re.compile(r"\b(?:phd|doctoral|doctorate|doctoral position)\b", re.I), "phd"),
    (re.compile(r"\b(?:master|mph|mphil|master'?s thesis)\b", re.I), "masters_mph"),
    (re.compile(r"\b(?:intern|internship|trainee|traineeship)\b", re.I), "internship"),
    (
        re.compile(r"\b(?:assistant|assistantship|teaching assistant|research assistant)\b", re.I),
        "assistantship",
    ),
    (
        re.compile(
            r"\b(?:fellow|fellowship|studentship|grant|scholarship|award|prize|premio|beca|ayuda|"
            r"borsa di ricerca|assegno di ricerca)\b",
            re.I,
        ),
        "research_fellowship",
    ),
    (re.compile(r"\bpost.?doc", re.I), "postdoc"),
    (
        re.compile(
            r"\b(?:research staff|researcher|research scientist|research engineer|research associate|"
            r"incarico di ricerca|incarico di lavoro autonomo)\b",
            re.I,
        ),
        "research_staff",
    ),
    (
        re.compile(
            r"\b(?:faculty|lecturer|professor|contratt[oi](?: integrativ[oi])? di insegnament[oi]|"
            r"incaric(?:o|hi) di insegnamento|carichi? di didattica)\b",
            re.I,
        ),
        "faculty",
    ),
    (
        re.compile(
            r"\b(?:academic|programme?|program|course|news|event|information page|other)\b",
            re.I,
        ),
        "other",
    ),
)

_DEEP_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_evidence_review",
        "description": (
            "Submit evidence findings for exactly one requested position. Call this tool once for every "
            "remaining position_id. The application validates IDs, enums and verbatim evidence."
        ),
        "parameters": {
            "type": "object",
            "required": [
                "position_id",
                "actual_vacancy",
                "open_status",
                "position_type",
                "opportunity_kind",
                "evidence_sufficient",
                "application_evidence",
                "negative_evidence",
                "confidence",
            ],
            "properties": {
                "position_id": {"type": "integer"},
                "actual_vacancy": {"type": "string", "enum": ["yes", "no", "unknown"]},
                "open_status": {
                    "type": "string",
                    "enum": ["open", "future", "closed", "unknown"],
                },
                "position_type": {"type": "string", "enum": list(POSITION_TYPES)},
                "opportunity_kind": {"type": "string", "enum": sorted(OPPORTUNITY_KINDS)},
                "evidence_sufficient": {"type": "boolean"},
                "application_evidence": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 10, "maxLength": 500},
                    "maxItems": 4,
                },
                "negative_evidence": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 10, "maxLength": 500},
                    "maxItems": 4,
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "additionalProperties": False,
        },
    },
}


class EvidenceDecision(BaseModel):
    position_id: int
    actual_vacancy: Literal["yes", "no", "unknown"]
    open_status: Literal["open", "future", "closed", "unknown"]
    position_type: str
    opportunity_kind: OpportunityKind
    evidence_sufficient: bool
    application_evidence: list[str] = Field(default_factory=list, max_length=4)
    negative_evidence: list[str] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0, le=1)


@dataclass(frozen=True, slots=True)
class DeepReviewResult:
    decision: EvidenceDecision
    attempts: int
    latency_seconds: float
    tool_error: bool = False
    validation_error: str | None = None
    ungrounded_decision: EvidenceDecision | None = None
    normalized_type_from: str | None = None
    rule_reason: str | None = None
    reused_from_position_id: int | None = None
    preflight_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _CachedVerdict:
    source_position_id: int
    accepted_status: Literal["eligible", "rejected"]
    decision: EvidenceDecision


def _review_fingerprint(position: Position, university: University | None) -> str:
    """Exact semantic identity; temporal and institutional fields invalidate reuse."""
    document = select_evidence_document(position.description, position.full_description)
    institution = (
        f"university:{university.id}:{university.name}:{university.country}"
        if university is not None
        else f"external:{position.institution_name or ''}:{position.institution_country or ''}"
    )
    fields = (
        compact_text(institution).casefold(),
        compact_text(position.title).casefold(),
        compact_text(document).casefold(),
        position.position_type or "other",
        getattr(position, "opportunity_kind", "unknown") or "unknown",
        position.deadline.isoformat() if position.deadline else "",
        compact_text(position.deadline_raw or "").casefold(),
        position.published_at.isoformat() if position.published_at else "",
        compact_text(position.published_raw or "").casefold(),
    )
    return hashlib.sha256("\x1f".join(fields).encode()).hexdigest()


def _stored_evidence(position: Position) -> list[str]:
    try:
        value = json.loads(position.screening_evidence or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _cached_verdict(position: Position) -> _CachedVerdict | None:
    if position.screening_status not in {"eligible", "rejected"}:
        return None
    evidence = _stored_evidence(position)
    if not evidence:
        return None
    context = build_evidence_context(
        f"{position.title}\n"
        f"{select_evidence_document(position.description, position.full_description)}"
    )
    if any(not evidence_quote_present(quote, context) for quote in evidence):
        return None
    eligible = position.screening_status == "eligible"
    accepted_status: Literal["eligible", "rejected"] = "eligible" if eligible else "rejected"
    if eligible and not application_evidence_supports(
        evidence,
        actual_vacancy="yes",
        open_status="open",
        position_type=position.position_type,
    ):
        return None
    opportunity_kind = getattr(position, "opportunity_kind", "unknown") or "unknown"
    if eligible and not opportunity_kind_evidence_supports(evidence, opportunity_kind):
        return None
    rejected_as_non_vacancy = not eligible and negative_evidence_supports(
        evidence,
        actual_vacancy="no",
        open_status="unknown",
    )
    rejected_as_closed = not eligible and negative_evidence_supports(
        evidence,
        actual_vacancy="unknown",
        open_status="closed",
    )
    if (
        not eligible
        and not rejected_as_non_vacancy
        and not rejected_as_closed
        and position.screening_source != "rules"
    ):
        return None
    decision = EvidenceDecision(
        position_id=position.id,
        actual_vacancy=(
            "yes" if eligible else "no" if rejected_as_non_vacancy else "unknown"
        ),
        open_status=("open" if eligible else "closed" if rejected_as_closed else "unknown"),
        position_type=position.position_type,
        opportunity_kind=opportunity_kind if eligible else "information" if rejected_as_non_vacancy else "vacancy",
        evidence_sufficient=True,
        application_evidence=evidence if eligible else [],
        negative_evidence=[] if eligible else evidence,
        confidence=position.screening_confidence or 1.0,
    )
    if _accepted_status(decision) != accepted_status:
        return None
    return _CachedVerdict(position.id, accepted_status, decision)


def _reuse_cached_result(position_id: int, cached: _CachedVerdict) -> DeepReviewResult:
    return DeepReviewResult(
        decision=cached.decision.model_copy(update={"position_id": position_id}),
        attempts=0,
        latency_seconds=0,
        reused_from_position_id=cached.source_position_id,
    )


class _IncompleteDeepReviewError(RuntimeError):
    def __init__(self, message: str, results: list[DeepReviewResult]) -> None:
        super().__init__(message)
        self.results = tuple(results)


class _EvidenceGroundingError(ValueError):
    """Structurally valid tool payload whose evidence cannot support its facts."""

    def __init__(self, message: str, decision: EvidenceDecision) -> None:
        super().__init__(message)
        self.decision = decision


class _UnknownPositionTypeError(ValueError):
    """Valid payload whose non-canonical type may be repaired after feedback."""

    def __init__(self, decision: EvidenceDecision) -> None:
        super().__init__(f"unknown position_type={decision.position_type!r}")
        self.decision = decision


def _tool_arguments(call: object) -> object:
    if isinstance(call, dict):
        function = call.get("function")
        if not isinstance(function, dict):
            raise ValueError("tool call has no function object")
        raw = function.get("arguments")
    else:
        function = getattr(call, "function", None)
        raw = getattr(function, "arguments", None)
    return json.loads(raw) if isinstance(raw, str) else raw


def _canonical_position_type(value: str) -> str | None:
    """Repair known human labels while rejecting genuinely unknown categories."""
    normalized = value.strip().casefold().replace("_", " ").replace("-", " ")
    for key, label in POSITION_TYPES.items():
        if normalized in (key.casefold().replace("_", " "), label.casefold()):
            return key
    return next((key for pattern, key in _TYPE_ALIASES if pattern.search(value)), None)


def _validated_decision(
    call: object,
    *,
    contexts: dict[int, str],
    accepted_ids: set[int],
    titles: dict[int, str] | None = None,
    application_anchors: dict[int, str] | None = None,
) -> EvidenceDecision | None:
    payload = _tool_arguments(call)
    # The tool schema already asks for at most four citations, but some local
    # models occasionally return a fifth. Keeping the first four is a bounded,
    # loss-only repair; all surviving claims still pass grounding below.
    if isinstance(payload, dict):
        payload = dict(payload)
        for field in ("application_evidence", "negative_evidence"):
            evidence = payload.get(field)
            if isinstance(evidence, list) and len(evidence) > 4:
                payload[field] = evidence[:4]
    decision = EvidenceDecision.model_validate(payload)
    if decision.position_id in accepted_ids:
        return None
    if decision.position_id not in contexts:
        raise ValueError(f"unexpected position_id={decision.position_id}")
    # An explicit abstention contains no factual claim to ground. Normalizing
    # it now avoids two pointless repair turns over quotes and type labels.
    if not decision.evidence_sufficient:
        return _grounding_abstention(decision.position_id)
    canonical_type = _canonical_position_type(decision.position_type)
    if canonical_type is None:
        raise _UnknownPositionTypeError(decision)
    if canonical_type != decision.position_type:
        decision = decision.model_copy(update={"position_type": canonical_type})
    return _validate_grounded_decision(
        decision,
        contexts[decision.position_id],
        title=(titles or {}).get(decision.position_id),
        application_anchor=(application_anchors or {}).get(decision.position_id),
    )


def _validate_grounded_decision(
    decision: EvidenceDecision,
    context: str,
    *,
    title: str | None = None,
    application_anchor: str | None = None,
) -> EvidenceDecision:
    """Validate quotes and semantics after schema/type normalization."""
    if not decision.evidence_sufficient:
        return _grounding_abstention(decision.position_id)

    supplied_evidence = [*decision.application_evidence, *decision.negative_evidence]
    valid_application = [
        quote for quote in decision.application_evidence if evidence_quote_present(quote, context)
    ]
    valid_negative = [
        quote for quote in decision.negative_evidence if evidence_quote_present(quote, context)
    ]
    valid_evidence = [*valid_application, *valid_negative]
    if supplied_evidence and not valid_evidence:
        raise _EvidenceGroundingError(
            f"evidence is not verbatim for position_id={decision.position_id}: {supplied_evidence[:2]}",
            decision,
        )
    if len(valid_evidence) != len(supplied_evidence):
        # Drop only malformed/redundant citations. The semantic validators
        # below must still establish every asserted fact from the exact subset.
        decision = decision.model_copy(
            update={
                "application_evidence": valid_application,
                "negative_evidence": valid_negative,
            }
        )

    if decision.actual_vacancy == "no" and decision.open_status != "unknown":
        raise _EvidenceGroundingError(
            "actual_vacancy=no requires open_status=unknown; a closed/open/future call is still "
            f"a concrete vacancy for position_id={decision.position_id}",
            decision,
        )
    if decision.actual_vacancy == "no" and decision.application_evidence:
        raise _EvidenceGroundingError(
            f"actual_vacancy=no cannot include application evidence for position_id={decision.position_id}",
            decision,
        )
    if decision.actual_vacancy == "yes" and decision.opportunity_kind not in {
        "vacancy",
        "programme",
        "spontaneous",
    }:
        raise _EvidenceGroundingError(
            "actual_vacancy=yes requires opportunity_kind=vacancy, programme or spontaneous for "
            f"position_id={decision.position_id}",
            decision,
        )

    if decision.evidence_sufficient and not valid_evidence:
        raise _EvidenceGroundingError(
            f"position_id={decision.position_id} claims sufficient evidence without quotes",
            decision,
        )
    # Local models sometimes cite the right clauses but put a future/open
    # application window in ``negative_evidence`` or omit the already supplied
    # candidate title. Repair only exact quotes and only when their combination
    # independently satisfies the complete positive semantic contract. A real
    # closed/negative clause makes ``application_evidence_supports`` fail and is
    # therefore never moved.
    current_kind = classify_opportunity_kind_evidence(valid_application)
    current_application_supported = application_evidence_supports(
        valid_application,
        actual_vacancy=decision.actual_vacancy,
        open_status=decision.open_status,
        position_type=decision.position_type,
    )
    if decision.actual_vacancy == "yes" and (
        not current_application_supported
        or (
            not opportunity_kind_evidence_supports(
                valid_application,
                decision.opportunity_kind,
            )
            and current_kind not in {"vacancy", "programme", "spontaneous"}
        )
    ):
        candidate_application = list(
            dict.fromkeys(
                [
                    *valid_application,
                    *(
                        valid_negative
                        if decision.open_status in {"open", "future"}
                        else []
                    ),
                ]
            )
        )
        if (
            title
            and title not in candidate_application
            and evidence_quote_present(title, context)
        ):
            candidate_application.insert(0, title)
        if (
            application_anchor
            and application_anchor not in candidate_application
            and evidence_quote_present(application_anchor, context)
        ):
            candidate_application.append(application_anchor)
        candidate_kind = classify_opportunity_kind_evidence(candidate_application)
        candidate_position_type = classify_position(
            " ".join(candidate_application),
            "",
        )
        if candidate_position_type == "other":
            candidate_position_type = decision.position_type
        if (
            len(candidate_application) <= 4
            and candidate_kind in {"vacancy", "programme", "spontaneous"}
            and application_evidence_supports(
                candidate_application,
                actual_vacancy=decision.actual_vacancy,
                open_status=decision.open_status,
                position_type=candidate_position_type,
            )
        ):
            moved_negative = decision.open_status in {"open", "future"}
            decision = decision.model_copy(
                update={
                    "application_evidence": candidate_application,
                    "negative_evidence": [] if moved_negative else valid_negative,
                    "position_type": candidate_position_type,
                }
            )
            valid_application = candidate_application
            if moved_negative:
                valid_negative = []
    if decision.open_status in {"open", "future"} and not relative_application_window_is_anchored(
        decision.application_evidence
    ):
        raise _EvidenceGroundingError(
            "relative application window has no grounded publication date or explicit current-open "
            f"signal for position_id={decision.position_id}. Quote the absolute publication/deadline "
            "date or a separate 'Apply now'/'currently open' clause; otherwise set open_status=unknown "
            "and evidence_sufficient=false",
            decision,
        )
    if decision.actual_vacancy == "yes" and decision.position_type == "other":
        grounded_type = classify_position(
            " ".join(decision.application_evidence),
            "",
            "other",
        )
        if grounded_type != "other":
            decision = decision.model_copy(update={"position_type": grounded_type})
    elif decision.actual_vacancy == "yes":
        # A canonical label can still be the wrong canonical label (for
        # example a local model calling an exact German ``stud. Hilfskraft``
        # an internship). Prefer the deterministic type exposed by the
        # model's own verbatim positive quotes. Unknown evidence leaves the
        # model label untouched and the complete validator remains decisive.
        grounded_type = classify_position(
            " ".join(decision.application_evidence),
            "",
        )
        if grounded_type != "other" and grounded_type != decision.position_type:
            decision = decision.model_copy(update={"position_type": grounded_type})
    if decision.actual_vacancy == "yes" and not opportunity_kind_evidence_supports(
        valid_application,
        decision.opportunity_kind,
    ):
        inferred_kind = classify_opportunity_kind_evidence(valid_application)
        if inferred_kind in {"vacancy", "programme", "spontaneous"}:
            # Opportunity kind is a routing label, not a user-visible factual
            # claim. If the model chose the wrong supported shape but its exact
            # quotes deterministically establish another one, repair the label
            # instead of wasting two more identical model calls.
            decision = decision.model_copy(update={"opportunity_kind": inferred_kind})
        else:
            raise _EvidenceGroundingError(
                "application evidence does not support the asserted opportunity_kind for "
                f"position_id={decision.position_id}. A vacancy needs a concrete role/call/project; a programme "
                "needs a named programme plus an application intake/window; a spontaneous lead needs an explicit "
                "unsolicited/speculative application or expression-of-interest instruction",
                decision,
            )
    if not application_evidence_supports(
        decision.application_evidence,
        actual_vacancy=decision.actual_vacancy,
        open_status=decision.open_status,
        position_type=decision.position_type,
    ):
        raise _EvidenceGroundingError(
            "application evidence does not support all asserted vacancy/open/type facts for "
            f"position_id={decision.position_id}. Quote the exact opportunity or role title and, "
            "for open/future calls, an exact application or deadline clause. If only closure is "
            "proven, use actual_vacancy=unknown; if decisive facts cannot be quoted, set "
            "evidence_sufficient=false",
            decision,
        )
    negative_validation_evidence = decision.negative_evidence
    if decision.open_status == "closed":
        # Models occasionally place the dated application deadline in the
        # positive array while still supplying a generic closure sentence in
        # the negative array. Facts are validated from the complete verbatim
        # evidence set; this does not weaken the requirement for at least one
        # negative citation before a rejected verdict can be composed.
        negative_validation_evidence = [
            *decision.negative_evidence,
            *decision.application_evidence,
        ]
    if not negative_evidence_supports(
        negative_validation_evidence,
        actual_vacancy=decision.actual_vacancy,
        open_status=decision.open_status,
    ):
        raise _EvidenceGroundingError(
            "negative evidence does not support the asserted non-vacancy/closed facts for "
            f"position_id={decision.position_id}. For closed calls quote an explicit closure or "
            "the complete dated application-deadline clause, never a bare date. If only closure "
            "is proven, use actual_vacancy=unknown",
            decision,
        )
    return decision


def _deterministic_rule_result(position: Position) -> DeepReviewResult | None:
    """Resolve only high-precision non-vacancy pages before spending GPU time."""
    document = select_evidence_document(
        position.description,
        position.full_description,
    )
    rule = screen_position(
        position.title,
        position.url,
        document,
        position.position_type,
    )
    if rule.status != "rejected":
        return None
    position_type = classify_position(
        position.title,
        document,
        position.position_type,
    )
    explicitly_closed = rule.reason == "explicitly_closed_or_unavailable"
    return DeepReviewResult(
        decision=EvidenceDecision(
            position_id=position.id,
            actual_vacancy="unknown" if explicitly_closed else "no",
            open_status="closed" if explicitly_closed else "unknown",
            position_type=position_type,
            opportunity_kind="vacancy" if explicitly_closed else "information",
            evidence_sufficient=True,
            application_evidence=[],
            negative_evidence=[position.title],
            confidence=1,
        ),
        attempts=0,
        latency_seconds=0,
        rule_reason=rule.reason,
    )


def _elapsed_deadline_rule_result(
    position: Position,
    *,
    today: date,
) -> DeepReviewResult | None:
    """Reject an expired, recognizable opportunity before spending GPU time."""
    deadline = position.deadline
    deadline_raw = position.deadline_raw
    document = select_evidence_document(position.description, position.full_description)
    extracted_raw, extracted_deadline = extract_deadline(document)
    if extracted_deadline is not None:
        # Re-evaluate persisted dates when the parser improves. Older parsing
        # could accidentally borrow a later interview/event date from the same
        # paragraph as the application deadline.
        deadline = extracted_deadline
        deadline_raw = extracted_raw
        position.deadline = deadline
        position.deadline_raw = deadline_raw
    if deadline is None or deadline >= today or not deadline_raw:
        return None

    position_type = classify_position(
        position.title,
        position.full_description or position.description,
        position.position_type,
    )
    if position_type == "other" or not application_evidence_supports(
        [position.title],
        actual_vacancy="yes",
        open_status="closed",
        position_type=position_type,
    ):
        return None
    context = build_evidence_context(
        f"{position.title}\n"
        f"{select_evidence_document(position.description, position.full_description)}"
    )
    if not evidence_quote_present(deadline_raw, context):
        return None
    return DeepReviewResult(
        decision=EvidenceDecision(
            position_id=position.id,
            actual_vacancy="yes",
            open_status="closed",
            position_type=position_type,
            opportunity_kind="vacancy",
            evidence_sufficient=True,
            application_evidence=[position.title],
            negative_evidence=[deadline_raw],
            confidence=1,
        ),
        attempts=0,
        latency_seconds=0,
        rule_reason="application_deadline_elapsed",
    )


def _grounding_abstention(position_id: int) -> EvidenceDecision:
    """Convert unsupported model facts into a safe, explicit abstention."""
    return EvidenceDecision(
        position_id=position_id,
        actual_vacancy="unknown",
        open_status="unknown",
        position_type="other",
        opportunity_kind="unknown",
        evidence_sufficient=False,
        application_evidence=[],
        negative_evidence=[],
        confidence=0,
    )


def _document_can_resolve(position: Position) -> bool:
    """Whether the deterministic validator could accept any model verdict.

    This mirrors the validator's minimum semantic requirements.  Calling the
    model when none of these signals exists can only end in an abstention, so
    skipping that call changes neither the accepted nor the rejected set.
    """
    context = build_evidence_context(
        f"{position.title}\n"
        f"{select_evidence_document(position.description, position.full_description)}"
    )
    positive_possible = application_evidence_supports(
        [context],
        actual_vacancy="yes",
        open_status="open",
        position_type="other",
    ) and any(
        opportunity_kind_evidence_supports([context], kind)
        for kind in ("vacancy", "programme", "spontaneous")
    )
    non_vacancy_possible = negative_evidence_supports(
        [context],
        actual_vacancy="no",
        open_status="unknown",
    )
    closed_possible = negative_evidence_supports(
        [context],
        actual_vacancy="unknown",
        open_status="closed",
    )
    return positive_possible or non_vacancy_possible or closed_possible


def _preflight_abstention(position: Position) -> DeepReviewResult | None:
    if _document_can_resolve(position):
        return None
    return DeepReviewResult(
        decision=_grounding_abstention(position.id),
        attempts=0,
        latency_seconds=0,
        preflight_reason="no_decisive_signals",
    )


def _raw_decision(decision: EvidenceDecision) -> Literal["eligible", "rejected", "review"]:
    """Verdetto fattuale prima delle soglie, conservato per audit e calibrazione."""
    if not decision.evidence_sufficient:
        return "review"
    if (
        decision.actual_vacancy == "yes"
        and decision.open_status in {"open", "future"}
        and decision.position_type != "other"
        and decision.opportunity_kind in {"vacancy", "programme", "spontaneous"}
        and decision.application_evidence
    ):
        return "eligible"
    if (decision.actual_vacancy == "no" or decision.open_status == "closed") and decision.negative_evidence:
        return "rejected"
    return "review"


def _accepted_status(decision: EvidenceDecision) -> Literal["eligible", "rejected", "review"]:
    """Composizione deterministica e asimmetrica: un falso reject costa di piu'."""
    raw_decision = _raw_decision(decision)
    if raw_decision == "eligible" and decision.confidence >= _ELIGIBLE_THRESHOLD:
        return "eligible"
    if raw_decision == "rejected" and (
        decision.confidence >= _REJECT_THRESHOLD
        or explicit_negative_evidence_supports(
            decision.negative_evidence,
            actual_vacancy=decision.actual_vacancy,
            open_status=decision.open_status,
        )
    ):
        return "rejected"
    return "review"


def _reason(decision: EvidenceDecision, status: str) -> str:
    facts = (
        f"vacancy={decision.actual_vacancy};open={decision.open_status};"
        f"kind={decision.opportunity_kind};evidence={'yes' if decision.evidence_sufficient else 'no'}"
    )
    return f"review2:{status}:{facts}"[:256]


def _bounded_cohort(checkpoint: dict[str, object], run_id: int | None) -> bool:
    """A limited review-1 run gives its append-only audited cohort priority."""
    return run_id is not None and checkpoint.get("cohort_complete") is False


def _prompt(
    rows: list[tuple[Position, University | None]],
    contexts: dict[int, str],
    *,
    today: date | None = None,
) -> str:
    candidates = [
        {
            "position_id": position.id,
            "title": position.title,
            "url": position.url,
            "institution": university.name if university else position.institution_name,
            "country": university.country if university else position.institution_country,
            "document": contexts[position.id],
        }
        for position, university in rows
    ]
    current_date = today or local_today()
    return (
        "Independently adjudicate each candidate using only its supplied document. You have not been given "
        "the first review and must not infer missing facts. Determine separately whether this is a concrete "
        "allowed academic opportunity, whether applications are open/future/closed, and its allowed position type. "
        "Pay special attention to negation, historical calls, generic programme pages and identifiers such as "
        "'No. 1'. Evidence strings must be short verbatim quotes from that candidate's document. If the page "
        "does not establish the decisive facts, set evidence_sufficient=false, set both factual states to unknown, "
        "use empty evidence arrays and confidence 0. A concrete historical or closed call is still an "
        "actual_vacancy=yes; actual_vacancy=no therefore requires open_status=unknown and no application evidence. "
        "For this tool, actual_vacancy=yes includes scholarships, fellowships, research or travel grants, awards, "
        "doctoral or Master's admissions/placements, internships and assistantships, even when applicants are "
        "already students. Generic programme/course pages, profiles, news and forms are not concrete opportunities. "
        "Classify opportunity_kind independently: vacancy is a specific advertised role/call/project; programme is "
        "a named degree or graduate programme with a current/future intake; spontaneous is an explicit unsolicited "
        "or speculative application/expression-of-interest route without an advertised slot; information is an "
        "evergreen procedural/news/FAQ page; unknown is an abstention. 'How to apply' proves only a procedure, not "
        "that a vacancy or intake is currently open. "
        f"Today's date is {current_date.isoformat()}. An explicit application deadline or application-window end "
        "before today means closed; quote the complete dated deadline clause in negative_evidence. Do not use "
        "publication, event, project or employment dates as application deadlines. "
        "A field saying 'Application Deadline: None specified' is not by itself proof that applications are open; "
        "quote a separate 'Apply now', 'How to apply', submission instruction or future application window. "
        "For a dedicated opportunity page, also quote its exact role/project/programme title or reference-number "
        "metadata. This applies equally to PhD projects, internships, assistantships and other allowed roles. "
        "When an 'Apply now' control is very short, include its adjacent exact words (for example the role title, "
        "mode of study or register-interest text) so the citation is attributable and not a generic navigation label. "
        "A relative window such as 'within 30 days from publication' cannot establish current openness unless "
        "you also quote an absolute publication/deadline date or a separate explicit current-open signal. "
        "When actual_vacancy=yes, application_evidence must name the vacancy and its type; when open_status is "
        "open/future it must also show an application or deadline signal. Use negative_evidence for explicit "
        "non-vacancy or closed/expired signals. Copy source wording exactly; Markdown link destinations, emphasis "
        "and outer quotation marks may be omitted, but never paraphrase, reorder words or drop negation. "
        "Use only these type meanings: phd for PhD/doctoral/predoctoral positions; masters_mph for Master's or MPH "
        "opportunities; medical_doctorate for medical doctorates; internship for internships/traineeships; "
        "assistantship for research/teaching assistantships; research_fellowship for fellowships, scholarships, "
        "awards and research/travel/conference grants, assegni or borse di ricerca; postdoc for postdoctoral posts; "
        "research_staff for researcher/scientist/engineer/associate "
        "or research-service contracts; faculty for professor/lecturer or teaching contracts; other only when none "
        "of those types is established. "
        "Call submit_evidence_review once per candidate. Do not provide a free-form answer.\n\n"
        f"{json.dumps(candidates, ensure_ascii=False)}"
    )


async def _request_batch(
    settings: Settings,
    rows: list[tuple[Position, University | None]],
    *,
    max_attempts: int = 3,
) -> list[DeepReviewResult]:
    model = settings.llm.model
    base_url = (settings.llm.api_base or "").rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    if not model.startswith("ollama/") or not base_url:
        raise RuntimeError(
            "review2 requires the configured local Ollama model and API base; no remote fallback is allowed"
        )
    contexts = {
        position.id: build_evidence_context(
            f"{position.title}\n"
            f"{select_evidence_document(position.description, position.full_description)}"
        )
        for position, _university in rows
    }
    titles = {position.id: position.title for position, _university in rows}
    today = local_today()
    application_anchors: dict[int, str] = {}
    for position, _university in rows:
        deadline_raw = position.deadline_raw
        deadline = position.deadline
        if (
            deadline_raw
            and deadline is not None
            and deadline >= today
            and extract_deadline(deadline_raw)[1] is not None
            and evidence_quote_present(deadline_raw, contexts[position.id])
        ):
            # The scraper already persisted this exact, position-specific
            # application clause.  Supplying it to the deterministic validator
            # lets us repair a model that returns only the bare date or puts the
            # clause in the negative field; the final semantic contract still
            # has to pass in full before a positive verdict is accepted.
            application_anchors[position.id] = deadline_raw
    expected_order = [position.id for position, _university in rows]
    messages: list[dict[str, Any]] = [{"role": "user", "content": _prompt(rows, contexts)}]
    accepted: dict[int, DeepReviewResult] = {}
    started = monotonic()
    last_error = "no valid tool call"

    for attempt in range(1, max_attempts + 1):
        # Most singleton dossiers need classification, not a long chain of
        # thought. Start and repair cheaply, then spend the larger reasoning
        # budget only on the final attempt. A truncated/invalid response is
        # never accepted: after the retry budget the item remains reviewable as
        # ``tool_error``.
        first_attempt = attempt == 1
        final_attempt = attempt == max_attempts
        payload = {
            "model": model.removeprefix("ollama/"),
            "messages": messages,
            "tools": [_DEEP_REVIEW_TOOL],
            "stream": False,
            "think": "high" if final_attempt else "low",
            "options": {
                "temperature": 0,
                "num_ctx": 32768,
                "num_predict": (
                    _FINAL_NUM_PREDICT
                    if final_attempt
                    else _INITIAL_NUM_PREDICT
                    if first_attempt
                    else _RETRY_NUM_PREDICT
                ),
            },
        }
        async with httpx.AsyncClient(timeout=900) as client:
            try:
                response = await client.post(f"{base_url}/api/chat", json=payload)
            except httpx.RequestError as exc:
                last_error = f"Ollama transport error: {type(exc).__name__}: {str(exc)[:300]}"
                await asyncio.sleep(min(2**attempt, 8))
                continue
        if response.status_code >= 400:
            last_error = f"Ollama HTTP {response.status_code}: {response.text[:500]}"
            await asyncio.sleep(min(2**attempt, 8))
            continue
        try:
            raw_message = response.json().get("message") or {}
            if not isinstance(raw_message, dict):
                raise ValueError("Ollama response message is not an object")
            message: dict[str, Any] = raw_message
            calls = list(message.get("tool_calls") or [])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            last_error = f"Invalid Ollama response: {type(exc).__name__}: {str(exc)[:300]}"
            await asyncio.sleep(min(2**attempt, 8))
            continue
        messages.append(message)

        errors: list[str] = []
        for call in calls:
            try:
                decision = _validated_decision(
                    call,
                    contexts=contexts,
                    accepted_ids=set(accepted),
                    titles=titles,
                    application_anchors=application_anchors,
                )
            except _UnknownPositionTypeError as exc:
                error = str(exc)[:500]
                position_id = exc.decision.position_id
                if attempt == max_attempts and position_id not in accepted:
                    # An unknown model label can describe either a genuine
                    # vacancy (for example "student" or "contract") or an
                    # administrative page.  Coercing it to a positive type
                    # could create a false eligible result.  ``other`` lets the
                    # grounded facts resolve an explicit closure, while every
                    # open/unclear case safely remains reviewable.
                    repaired = exc.decision.model_copy(update={"position_type": "other"})
                    try:
                        repaired = _validate_grounded_decision(
                            repaired,
                            contexts[position_id],
                            title=titles.get(position_id),
                            application_anchor=application_anchors.get(position_id),
                        )
                    except _EvidenceGroundingError as grounding_error:
                        accepted[position_id] = DeepReviewResult(
                            decision=_grounding_abstention(position_id),
                            attempts=attempt,
                            latency_seconds=monotonic() - started,
                            validation_error=str(grounding_error)[:500],
                            ungrounded_decision=repaired,
                            normalized_type_from=exc.decision.position_type,
                        )
                    else:
                        accepted[position_id] = DeepReviewResult(
                            decision=repaired,
                            attempts=attempt,
                            latency_seconds=monotonic() - started,
                            normalized_type_from=exc.decision.position_type,
                        )
                else:
                    errors.append(error)
                continue
            except _EvidenceGroundingError as exc:
                error = str(exc)[:500]
                # Two normal feedback rounds still give the model a chance to
                # repair its citations.  On the final round, a structurally
                # valid but unsupported verdict becomes an abstention rather
                # than a technical failure or a fabricated decision.
                if attempt == max_attempts and exc.decision.position_id not in accepted:
                    accepted[exc.decision.position_id] = DeepReviewResult(
                        decision=_grounding_abstention(exc.decision.position_id),
                        attempts=attempt,
                        latency_seconds=monotonic() - started,
                        validation_error=error,
                        ungrounded_decision=exc.decision,
                    )
                else:
                    errors.append(error)
                continue
            except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
                errors.append(str(exc)[:500])
                continue
            if decision is not None:
                accepted[decision.position_id] = DeepReviewResult(
                    decision=decision,
                    attempts=attempt,
                    latency_seconds=monotonic() - started,
                )

        remaining = [position_id for position_id in expected_order if position_id not in accepted]
        if not remaining:
            return [accepted[position_id] for position_id in expected_order]
        last_error = "; ".join(errors) if errors else "missing tool calls"
        feedback = json.dumps(
            {
                "ok": False,
                "accepted_ids": sorted(accepted),
                "remaining_ids": remaining,
                "error": last_error,
                "instruction": "Do not repeat accepted IDs. Correct and call the tool once for each remaining ID.",
            },
            ensure_ascii=False,
        )
        if calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_name": "submit_evidence_review",
                    "content": feedback,
                }
            )
        else:
            messages.append({"role": "user", "content": feedback})

    raise _IncompleteDeepReviewError(last_error, list(accepted.values()))


def _tool_failure(
    position_id: int,
    *,
    latency_seconds: float,
    validation_error: str | None = None,
) -> DeepReviewResult:
    return DeepReviewResult(
        decision=EvidenceDecision(
            position_id=position_id,
            actual_vacancy="unknown",
            open_status="unknown",
            position_type="other",
            opportunity_kind="unknown",
            evidence_sufficient=False,
            application_evidence=[],
            negative_evidence=[],
            confidence=0,
        ),
        attempts=3,
        latency_seconds=latency_seconds,
        tool_error=True,
        validation_error=validation_error,
    )


def _result_version(result: DeepReviewResult, previous_version: str | None) -> str | None:
    """Technical failures remain eligible for a later review2 run."""
    return previous_version if result.tool_error else REVIEW_VERSION


def _review_state(result: DeepReviewResult, accepted_status: str) -> str:
    if accepted_status != "review":
        return "resolved"
    if result.tool_error:
        return "tool_error"
    if result.ungrounded_decision is not None:
        return "grounding_failure"
    if not result.decision.evidence_sufficient:
        return "source_unusable"
    return "human_review"


def _apply_elapsed_deadline_guard(
    result: DeepReviewResult,
    position: Position,
    *,
    today: date,
) -> DeepReviewResult:
    """Correct an open/future claim when a sourced application deadline elapsed.

    The guard runs only after a grounded positive decision. The dated clause is
    extracted through a lexical deadline gate, so unrelated event, publication
    and project dates cannot trigger it.
    """
    decision = result.decision
    if (
        result.rule_reason is not None
        or decision.actual_vacancy != "yes"
        or decision.open_status not in {"open", "future"}
        or not decision.evidence_sufficient
    ):
        return result

    deadline = position.deadline
    deadline_raw = position.deadline_raw
    cited_raw, cited_deadline = extract_deadline(
        "\n".join(decision.application_evidence)
    )
    document = select_evidence_document(position.description, position.full_description)
    document_raw, document_deadline = extract_deadline(document)
    if cited_deadline is not None:
        deadline = cited_deadline
        deadline_raw = cited_raw
    elif document_deadline is not None:
        deadline = document_deadline
        deadline_raw = document_raw
    if deadline is not None and (
        deadline != position.deadline or deadline_raw != position.deadline_raw
    ):
        position.deadline = deadline
        position.deadline_raw = deadline_raw
    if deadline is None or deadline >= today or not deadline_raw:
        return result

    context = build_evidence_context(
        f"{position.title}\n"
        f"{select_evidence_document(position.description, position.full_description)}"
    )
    if not evidence_quote_present(deadline_raw, context):
        return result
    negative_evidence = list(dict.fromkeys([*decision.negative_evidence, deadline_raw]))[:4]
    guarded = decision.model_copy(
        update={
            "open_status": "closed",
            "negative_evidence": negative_evidence,
            "confidence": 1.0,
        }
    )
    return replace(
        result,
        decision=guarded,
        rule_reason="application_deadline_elapsed",
    )


def _audit_details(result: DeepReviewResult) -> dict[str, object]:
    """Keep the rejected model facts for diagnostics, never as current facts."""
    decision = result.decision
    return {
        "actual_vacancy": decision.actual_vacancy,
        "open_status": decision.open_status,
        "opportunity_kind": decision.opportunity_kind,
        "evidence_sufficient": decision.evidence_sufficient,
        "tool_error": result.tool_error,
        "validation_error": result.validation_error,
        "normalized_type_from": result.normalized_type_from,
        "rule_reason": result.rule_reason,
        "reused_from_position_id": result.reused_from_position_id,
        "preflight_reason": result.preflight_reason,
        "validation_kind": (
            "deterministic_rule"
            if result.rule_reason is not None
            else "preflight_abstention"
            if result.preflight_reason is not None
            else "tool_error"
            if result.tool_error
            else "grounding_failure"
            if result.ungrounded_decision is not None
            else None
        ),
        "ungrounded_decision": (
            result.ungrounded_decision.model_dump(mode="json")
            if result.ungrounded_decision is not None
            else None
        ),
    }


async def _resilient_review(
    settings: Settings,
    rows: list[tuple[Position, University | None]],
) -> list[DeepReviewResult]:
    started = monotonic()
    try:
        return await _request_batch(settings, rows)
    except _IncompleteDeepReviewError as exc:
        accepted = {result.decision.position_id: result for result in exc.results}
        unresolved = [row for row in rows if row[0].id not in accepted]
        if len(unresolved) > 1:
            midpoint = len(unresolved) // 2
            recovered = [
                *await _resilient_review(settings, unresolved[:midpoint]),
                *await _resilient_review(settings, unresolved[midpoint:]),
            ]
        elif unresolved and accepted:
            # The mixed prompt exhausted its budget, but the last unresolved
            # item still gets a clean singleton context before quarantine.
            recovered = await _resilient_review(settings, unresolved)
        elif unresolved:
            recovered = [
                _tool_failure(
                    unresolved[0][0].id,
                    latency_seconds=monotonic() - started,
                    validation_error=str(exc)[:500],
                )
            ]
        else:
            recovered = []
        by_id = {result.decision.position_id: result for result in [*accepted.values(), *recovered]}
        return [by_id[position.id] for position, _university in rows]


def _deep_review_candidate_filter(today: date) -> ColumnElement[bool]:
    """Select only current records that can materially benefit from adjudication."""
    current = or_(Position.deadline.is_(None), Position.deadline >= today)
    return or_(
        and_(
            current,
            Position.screening_status == "review",
            or_(
                Position.review_state.in_(_RETRYABLE_REVIEW_STATES),
                and_(
                    Position.review_state.in_(_ONE_TIME_RETRY_REVIEW_STATES),
                    or_(
                        Position.screening_version.is_(None),
                        Position.screening_version != REVIEW_VERSION,
                    ),
                ),
                and_(
                    Position.screening_version.in_(_THRESHOLD_RETRY_VERSIONS),
                    Position.screening_decision == "eligible",
                    Position.screening_confidence >= _ELIGIBLE_THRESHOLD,
                ),
                Position.screening_version.is_(None),
                Position.screening_version.notin_(_REVIEW_COMPATIBLE_VERSIONS),
            ),
        ),
        and_(
            current,
            Position.screening_status == "rejected",
            Position.screening_manual.is_(False),
            Position.screening_source.in_(("llm", "cache")),
            Position.screening_version.in_(_REQUEUE_REJECTED_VERSIONS),
        ),
        and_(
            current,
            Position.screening_status == "eligible",
            Position.screening_manual.is_(False),
            Position.screening_source.in_(("llm", "cache")),
            Position.screening_version.in_(_REQUEUE_ELIGIBLE_VERSIONS),
        ),
    )


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    """Rivede soltanto gli incerti per cui esiste nuova evidenza di dettaglio."""
    progress = progress or Progress()
    settings = container.get(Settings)
    session_maker = container.get(async_sessionmaker[AsyncSession])
    today = local_today()
    checkpoint = await progress.load_checkpoint()
    raw_processed = checkpoint.get("processed", 0)
    processed = (
        raw_processed
        if isinstance(raw_processed, int)
        else int(raw_processed)
        if isinstance(raw_processed, str) and raw_processed.isdigit()
        else 0
    )
    remaining = None if limit is None else max(limit - processed, 0)
    bounded_cohort = _bounded_cohort(
        await progress.load_stage_checkpoint("review"),
        progress.run_id,
    )

    async with session_maker() as session:
        swept = await apply_rule_sweep(
            session,
            pipeline_run_id=progress.run_id,
            name_like=name_like,
        )
        if swept:
            await progress.save_checkpoint(rule_sweep_rejected=swept)
        resolved_stmt = (
            select(Position, University)
            .outerjoin(University, Position.university_id == University.id)
            .where(
                Position.is_active.is_(True),
                Position.screening_manual.is_(False),
                Position.screening_status.in_(("eligible", "rejected")),
                Position.screening_version.in_(_CACHE_COMPATIBLE_VERSIONS),
            )
            .order_by(Position.id)
        )
        resolved_rows: list[tuple[Position, University | None]] = [
            (position, university)
            for position, university in (await session.execute(resolved_stmt)).all()
        ]
        verdict_cache: dict[str, _CachedVerdict] = {}
        conflicted_fingerprints: set[str] = set()
        for resolved_position, resolved_university in resolved_rows:
            cached = _cached_verdict(resolved_position)
            if cached is None:
                continue
            fingerprint = _review_fingerprint(resolved_position, resolved_university)
            previous = verdict_cache.get(fingerprint)
            if previous is not None and previous.accepted_status != cached.accepted_status:
                verdict_cache.pop(fingerprint, None)
                conflicted_fingerprints.add(fingerprint)
            elif fingerprint not in conflicted_fingerprints:
                verdict_cache[fingerprint] = cached
        review_priority = case(
            (Position.screening_version.in_(("evidence-v10", "evidence-v11")), 0),
            (Position.screening_status.in_(("eligible", "rejected")), 1),
            (Position.review_state == "grounding_failure", 2),
            (Position.review_state == "tool_error", 3),
            # Freshly fetched, attributable detail pages are the highest-yield
            # new work.  Keep them ahead of known-unusable sources so a bounded
            # overnight run spends its GPU budget on resolvable candidates.
            (Position.review_state == "ready_deep_review", 4),
            (Position.review_state == "human_review", 5),
            (Position.review_state == "source_unusable", 7),
            else_=6,
        )
        stmt = (
            select(Position, University)
            .outerjoin(University, Position.university_id == University.id)
            .where(
                Position.is_active.is_(True),
                Position.screening_manual.is_(False),
                _deep_review_candidate_filter(today),
                Position.full_description.is_not(None),
            )
        )
        if name_like:
            stmt = stmt.where(
                or_(University.name.ilike(f"%{name_like}%"), Position.institution_name.ilike(f"%{name_like}%"))
            )
        if bounded_cohort:
            cohort = select(ReviewAttempt.position_id).where(
                ReviewAttempt.pipeline_run_id == progress.run_id,
                ReviewAttempt.stage == "review",
            )
            # A pilot must handle its own review-1 residue first, but excluding
            # durable backlog here stranded ready/tool-error and legacy rows in
            # every later limited run. The stage limit applies after this
            # priority, so spare capacity continues the global queue safely.
            cohort_priority = case((Position.id.in_(cohort), 0), else_=1)
            stmt = stmt.order_by(
                cohort_priority,
                review_priority,
                Position.screened_at.asc().nullsfirst(),
                Position.id,
            )
        else:
            # Tool failures deliberately keep their previous version so they
            # can retry. Ordering by oldest attempt prevents one persistently
            # malformed source from starving the rest of the queue.
            stmt = stmt.order_by(
                review_priority,
                Position.screened_at.asc().nullsfirst(),
                Position.id,
            )
        if remaining is not None:
            stmt = stmt.limit(remaining)
        rows: list[tuple[Position, University | None]] = [
            (position, university) for position, university in (await session.execute(stmt)).all()
        ]
        await progress.begin((len(rows) + _BATCH_SIZE - 1) // _BATCH_SIZE)

        for offset in range(0, len(rows), _BATCH_SIZE):
            batch = rows[offset : offset + _BATCH_SIZE]
            await progress.tick(f"evidence review {processed + 1}-{processed + len(batch)}")
            rule_results = [
                result
                for position, _university in batch
                if (
                    result := _deterministic_rule_result(position)
                    or _elapsed_deadline_rule_result(position, today=today)
                ) is not None
            ]
            rule_ids = {result.decision.position_id for result in rule_results}
            cached_results: list[DeepReviewResult] = []
            for position, university in batch:
                if position.id in rule_ids:
                    continue
                cached = verdict_cache.get(_review_fingerprint(position, university))
                if cached is not None and cached.source_position_id != position.id:
                    cached_results.append(_reuse_cached_result(position.id, cached))
            cached_ids = {result.decision.position_id for result in cached_results}
            preflight_results = [
                result
                for position, _university in batch
                if position.id not in rule_ids
                and position.id not in cached_ids
                and (result := _preflight_abstention(position)) is not None
            ]
            preflight_ids = {
                result.decision.position_id for result in preflight_results
            }
            model_batch = [
                row
                for row in batch
                if row[0].id not in rule_ids
                and row[0].id not in cached_ids
                and row[0].id not in preflight_ids
            ]
            model_results = await _resilient_review(settings, model_batch) if model_batch else []
            results = [
                *rule_results,
                *cached_results,
                *preflight_results,
                *model_results,
            ]
            positions_by_id = {position.id: position for position, _university in batch}
            results = [
                _apply_elapsed_deadline_guard(
                    result,
                    positions_by_id[result.decision.position_id],
                    today=today,
                )
                for result in results
            ]
            by_id = {result.decision.position_id: result for result in results}
            reviewed_at = datetime.now(UTC).replace(tzinfo=None)
            for position, _university in batch:
                result = by_id[position.id]
                decision = result.decision
                raw_decision = _raw_decision(decision)
                accepted_status = _accepted_status(decision)
                evidence = [*decision.application_evidence, *decision.negative_evidence]
                position.screening_status = accepted_status
                position.position_type = classify_position(
                    position.title,
                    position.full_description or position.description,
                    decision.position_type,
                )
                position.opportunity_kind = decision.opportunity_kind
                position.screening_reason = (
                    f"review2:rule:{result.rule_reason}"[:256]
                    if result.rule_reason is not None
                    else f"review2:cache:{result.reused_from_position_id}"
                    if result.reused_from_position_id is not None
                    else _reason(decision, accepted_status)
                )
                position.screening_source = (
                    "rules"
                    if result.rule_reason is not None
                    else "cache"
                    if result.reused_from_position_id is not None
                    else "router"
                    if result.preflight_reason is not None
                    else "llm"
                )
                position.screening_decision = raw_decision
                position.screening_confidence = decision.confidence
                position.screening_evidence = json.dumps(evidence, ensure_ascii=False)
                position.screening_model = (
                    None
                    if result.rule_reason is not None
                    or result.reused_from_position_id is not None
                    or result.preflight_reason is not None
                    else settings.llm.model
                )
                position.screening_version = _result_version(result, position.screening_version)
                position.screened_at = reviewed_at
                position.review_state = _review_state(result, accepted_status)
                position.routing_reason = (
                    f"review2:rule:{result.rule_reason}"
                    if result.rule_reason is not None
                    else f"review2:cache:{result.reused_from_position_id}"
                    if result.reused_from_position_id is not None
                    else "review2:tool_error"
                    if result.tool_error
                    else "review2:grounding_failure"
                    if position.review_state == "grounding_failure"
                    else "review2:source_unusable"
                    if position.review_state == "source_unusable"
                    else position.screening_reason
                )
                position.indexed_at = None
                append_review_attempt(
                    session,
                    position_id=position.id,
                    pipeline_run_id=progress.run_id,
                    stage="review2",
                    model=(
                        None
                        if result.rule_reason is not None
                        or result.reused_from_position_id is not None
                        or result.preflight_reason is not None
                        else settings.llm.model
                    ),
                    version=REVIEW_VERSION,
                    raw_decision=raw_decision,
                    accepted_status=accepted_status,
                    position_type=decision.position_type,
                    confidence=decision.confidence,
                    evidence=evidence,
                    reason=position.screening_reason,
                    tool_attempts=result.attempts,
                    latency_seconds=result.latency_seconds,
                    details=_audit_details(result),
                )
                if accepted_status in {"eligible", "rejected"}:
                    fingerprint = _review_fingerprint(position, _university)
                    cached = _cached_verdict(position)
                    previous = verdict_cache.get(fingerprint)
                    if (
                        cached is not None
                        and fingerprint not in conflicted_fingerprints
                        and (previous is None or previous.accepted_status == cached.accepted_status)
                    ):
                        verdict_cache[fingerprint] = cached
                    elif cached is not None and previous is not None:
                        verdict_cache.pop(fingerprint, None)
                        conflicted_fingerprints.add(fingerprint)
            await session.commit()
            processed += len(batch)
            await progress.save_checkpoint(processed=processed, last_position_id=batch[-1][0].id)
            await progress.check_stop()
            if progress.should_stop:
                break
    return processed
