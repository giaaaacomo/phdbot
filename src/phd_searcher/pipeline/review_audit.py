"""Helper unico per lo storico append-only della review automatica."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from phd_searcher.database.models.review_attempt import ReviewAttempt


def append_review_attempt(
    session: AsyncSession,
    *,
    position_id: int,
    pipeline_run_id: int | None,
    stage: str,
    model: str | None,
    version: str,
    raw_decision: str,
    accepted_status: str,
    position_type: str | None,
    confidence: float | None,
    evidence: list[str],
    reason: str | None,
    tool_attempts: int = 1,
    latency_seconds: float | None = None,
    details: dict[str, object] | None = None,
) -> ReviewAttempt:
    attempt = ReviewAttempt(
        position_id=position_id,
        pipeline_run_id=pipeline_run_id,
        stage=stage,
        model=model,
        version=version,
        raw_decision=raw_decision,
        accepted_status=accepted_status,
        position_type=position_type,
        confidence=confidence,
        evidence=evidence,
        reason=reason,
        tool_attempts=tool_attempts,
        latency_seconds=latency_seconds,
        details=details or {},
    )
    session.add(attempt)
    return attempt
