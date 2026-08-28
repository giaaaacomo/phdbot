"""Versioned, high-precision rule sweep before network and GPU work."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from phd_searcher.clock import local_today
from phd_searcher.database.models.pipeline_run import PipelineRun
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.pipeline.review_audit import append_review_attempt
from phd_searcher.pipeline.review_context import (
    application_evidence_supports,
    opportunity_kind_evidence_supports,
    select_evidence_document,
)
from phd_searcher.screening import ScreeningDecision, screen_position

RULE_SWEEP_VERSION = "rules-v13"

_GENERIC_PAGE_REJECTIONS = frozenset({"navigation_link", "non_opportunity_page"})
_GROUNDED_POSITIVE_SOURCES = frozenset({"llm", "cache"})


def _parsed_evidence(raw_evidence: object) -> list[str]:
    if not isinstance(raw_evidence, str):
        return []
    try:
        evidence = json.loads(raw_evidence)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(evidence, list):
        return []
    return [item for item in evidence if isinstance(item, str) and item.strip()]


def _is_reliable_grounded_eligible(position: Position) -> bool:
    """Recognize positives already accepted by the evidence-grounded reviewer."""
    version = getattr(position, "screening_version", None)
    confidence = getattr(position, "screening_confidence", None)
    evidence = _parsed_evidence(getattr(position, "screening_evidence", None))
    metadata_is_reliable = bool(
        getattr(position, "screening_status", None) == "eligible"
        and getattr(position, "screening_source", None)
        in _GROUNDED_POSITIVE_SOURCES
        and isinstance(version, str)
        and version.startswith("evidence-v")
        and getattr(position, "screening_decision", None) == "eligible"
        and isinstance(confidence, (float, int))
        and confidence >= 0.9
    )
    if not metadata_is_reliable or not evidence:
        return False
    position_type = getattr(position, "position_type", "other") or "other"
    opportunity_kind = getattr(position, "opportunity_kind", "unknown") or "unknown"
    return application_evidence_supports(
        evidence,
        actual_vacancy="yes",
        open_status="open",
        position_type=position_type,
    ) and opportunity_kind_evidence_supports(evidence, opportunity_kind)


def _should_apply_rejection(
    position: Position, decision: ScreeningDecision
) -> bool:
    """Let explicit contradictions override positives, never a generic label."""
    if decision.status != "rejected":
        return False
    return not (
        decision.reason in _GENERIC_PAGE_REJECTIONS
        and _is_reliable_grounded_eligible(position)
    )


def _should_requeue_obsolete_rejection(
    position: Position,
    decision: ScreeningDecision,
) -> bool:
    """Reopen only a rule rejection that the current rule set no longer supports."""
    return bool(
        getattr(position, "screening_status", None) == "rejected"
        and getattr(position, "screening_source", None) == "rules"
        and getattr(position, "screening_version", None) != RULE_SWEEP_VERSION
        and decision.status != "rejected"
    )


def _coerce_limit(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _configured_sweep_limit(
    params: object,
    current_stage: object,
) -> int | None:
    """Mirror the active stage limit so pilots cannot trigger a global sweep."""
    if not isinstance(params, dict):
        return None
    stage_limits = params.get("limits")
    if (
        isinstance(stage_limits, dict)
        and isinstance(current_stage, str)
        and current_stage in stage_limits
    ):
        return _coerce_limit(stage_limits[current_stage])
    return _coerce_limit(params.get("limit"))


async def _run_sweep_limit(
    session: AsyncSession,
    pipeline_run_id: int | None,
) -> int | None:
    if pipeline_run_id is None:
        return None
    pipeline_run = await session.get(PipelineRun, pipeline_run_id)
    if pipeline_run is None:
        return None
    return _configured_sweep_limit(
        getattr(pipeline_run, "params", None),
        getattr(pipeline_run, "current_stage", None),
    )


async def _requeue_obsolete_rule_rejections(
    session: AsyncSession,
    *,
    pipeline_run_id: int | None,
    name_like: str | None,
    limit: int | None,
) -> int:
    """Undo legacy rule false negatives without restoring an eligible verdict.

    A newer rule set may learn that a generic heading actually contains a
    concrete current call. Reopening it as ``review`` is reversible and lets
    the evidence reviewer adjudicate it; it never reinstates the old positive
    automatically.
    """
    today = local_today()
    stmt = (
        select(Position)
        .outerjoin(University, Position.university_id == University.id)
        .where(
            Position.is_active.is_(True),
            Position.screening_manual.is_(False),
            Position.screening_status == "rejected",
            Position.screening_source == "rules",
            or_(
                Position.screening_version.is_(None),
                Position.screening_version != RULE_SWEEP_VERSION,
            ),
            or_(Position.deadline.is_(None), Position.deadline >= today),
        )
        .order_by(Position.id)
    )
    if name_like:
        stmt = stmt.where(
            or_(
                University.name.ilike(f"%{name_like}%"),
                Position.institution_name.ilike(f"%{name_like}%"),
            )
        )
    if limit is not None:
        stmt = stmt.limit(limit)
    candidates = (await session.execute(stmt)).scalars().all()
    reopened = 0
    reviewed_at = datetime.now(UTC).replace(tzinfo=None)
    for position in candidates:
        decision = sweep_decision(position)
        if not _should_requeue_obsolete_rejection(position, decision):
            continue
        old_version = position.screening_version
        old_reason = position.screening_reason
        reason = "rule_sweep:obsolete_rejection_requeued"
        position.screening_status = "review"
        position.screening_reason = reason
        position.screening_source = "router"
        position.screening_decision = "review"
        position.screening_confidence = None
        position.screening_evidence = "[]"
        position.screening_model = None
        position.screening_version = RULE_SWEEP_VERSION
        position.screened_at = reviewed_at
        position.review_state = (
            "ready_deep_review"
            if position.full_description
            else "needs_evidence"
        )
        position.routing_reason = reason
        position.indexed_at = None
        append_review_attempt(
            session,
            position_id=position.id,
            pipeline_run_id=pipeline_run_id,
            stage="rules",
            model=None,
            version=RULE_SWEEP_VERSION,
            raw_decision="review",
            accepted_status="review",
            position_type=position.position_type,
            confidence=None,
            evidence=[],
            reason=reason,
            tool_attempts=0,
            latency_seconds=0,
            details={
                "rule_reason": "obsolete_rejection_requeued",
                "previous_version": old_version,
                "previous_reason": old_reason,
                "current_rule_status": decision.status,
                "current_rule_reason": decision.reason,
            },
        )
        reopened += 1
    if reopened:
        await session.commit()
    return reopened


def sweep_decision(position: Position) -> ScreeningDecision:
    """Evaluate the best attributable text without changing the position."""
    return screen_position(
        position.title,
        position.url,
        select_evidence_document(
            position.description,
            position.full_description,
            title=position.title,
            url=position.url,
            deadline=getattr(position, "deadline", None),
            deadline_raw=getattr(position, "deadline_raw", None),
        ),
        position.position_type,
    )


async def apply_rule_sweep(
    session: AsyncSession,
    *,
    pipeline_run_id: int | None,
    name_like: str | None = None,
) -> int:
    """Reject deterministic non-opportunities among non-manual active records.

    Non-matches are intentionally not stamped: a newer rule version can inspect
    them again cheaply, while the audit table only grows when a verdict changes.
    Existing eligible records are included so newer high-precision rules can
    repair legacy false positives without waiting for them to be scraped again.
    """
    today = local_today()
    sweep_limit = await _run_sweep_limit(session, pipeline_run_id)
    reopened = await _requeue_obsolete_rule_rejections(
        session,
        pipeline_run_id=pipeline_run_id,
        name_like=name_like,
        limit=sweep_limit,
    )
    remaining_sweep_limit = (
        None if sweep_limit is None else max(sweep_limit - reopened, 0)
    )
    stmt = (
        select(Position)
        .outerjoin(University, Position.university_id == University.id)
        .where(
            Position.is_active.is_(True),
            Position.screening_manual.is_(False),
            Position.screening_status.in_(("review", "eligible")),
            or_(Position.deadline.is_(None), Position.deadline >= today),
        )
        .order_by(Position.id)
    )
    if name_like:
        stmt = stmt.where(
            or_(
                University.name.ilike(f"%{name_like}%"),
                Position.institution_name.ilike(f"%{name_like}%"),
            )
        )
    if remaining_sweep_limit is not None:
        stmt = stmt.limit(remaining_sweep_limit)
    candidates = (await session.execute(stmt)).scalars().all()
    changed = 0
    reviewed_at = datetime.now(UTC).replace(tzinfo=None)
    for position in candidates:
        decision = sweep_decision(position)
        if not _should_apply_rejection(position, decision):
            continue
        reason = f"rule_sweep:{decision.reason}"[:256]
        evidence = [position.title]
        position.screening_status = "rejected"
        position.screening_reason = reason
        position.screening_source = "rules"
        position.screening_decision = "rejected"
        position.screening_confidence = 1.0
        position.screening_evidence = json.dumps(evidence, ensure_ascii=False)
        position.screening_model = None
        position.screening_version = RULE_SWEEP_VERSION
        position.screened_at = reviewed_at
        position.review_state = "resolved"
        position.routing_reason = reason
        position.indexed_at = None
        append_review_attempt(
            session,
            position_id=position.id,
            pipeline_run_id=pipeline_run_id,
            stage="rules",
            model=None,
            version=RULE_SWEEP_VERSION,
            raw_decision="rejected",
            accepted_status="rejected",
            position_type=position.position_type,
            confidence=1.0,
            evidence=evidence,
            reason=reason,
            tool_attempts=0,
            latency_seconds=0,
            details={"rule_reason": decision.reason},
        )
        changed += 1
    if changed:
        await session.commit()
    return changed
