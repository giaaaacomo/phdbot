from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from phd_searcher.database.models.scheduled_job import ScheduledJob
from phd_searcher.pipeline.runner import PipelineError
from phd_searcher.service.schedule_service import (
    ScheduleService,
    _due_jobs_statement,
    _scheduled_pipeline_retry_delay,
    _to_utc_naive,
)
from phd_searcher.typedef.pipeline import PipelineStartBody
from phd_searcher.typedef.schedule import ScheduleCreate


def test_rome_schedule_converts_summer_and_winter_to_utc() -> None:
    assert _to_utc_naive(datetime(2026, 8, 18, 19, 0)) == datetime(2026, 8, 18, 17, 0)
    assert _to_utc_naive(datetime(2026, 1, 18, 19, 0)) == datetime(2026, 1, 18, 18, 0)


def test_rome_schedule_rejects_nonexistent_and_ambiguous_dst_times() -> None:
    with pytest.raises(ValueError, match="does not exist"):
        _to_utc_naive(datetime(2026, 3, 29, 2, 30))
    with pytest.raises(ValueError, match="ambiguous"):
        _to_utc_naive(datetime(2026, 10, 25, 2, 30))


def test_schedule_create_requires_payload_matching_target() -> None:
    pipeline = ScheduleCreate(
        target="pipeline",
        run_at=datetime(2026, 8, 18, 19, 0),
        pipeline=PipelineStartBody(stages=["evidence", "review2", "index"]),
    )
    assert pipeline.timezone == "Europe/Rome"
    with pytest.raises(ValidationError, match="pipeline parameters"):
        ScheduleCreate(target="pipeline", run_at=datetime(2026, 8, 18, 19, 0))
    with pytest.raises(ValidationError, match="macro_id"):
        ScheduleCreate(target="macro", run_at=datetime(2026, 8, 18, 19, 0))
    with pytest.raises(ValidationError):
        ScheduleCreate(
            target="pipeline",
            run_at=datetime(2026, 8, 18, 19, 0),
            timezone="Europe/London",  # type: ignore[arg-type]
            pipeline=PipelineStartBody(),
        )


def test_due_job_claim_is_cluster_safe_and_lease_aware() -> None:
    sql = str(
        _due_jobs_statement(datetime(2026, 8, 18, 17, 0)).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "scheduled_jobs.lease_until" in sql
    assert "scheduled_jobs.next_attempt_at" in sql


def test_scheduled_pipeline_retries_only_bounded_transient_failures() -> None:
    assert _scheduled_pipeline_retry_delay("HTTP 429 rate limit", 1) == 900
    assert _scheduled_pipeline_retry_delay("connection reset by peer", 3) == 300
    assert _scheduled_pipeline_retry_delay("connection reset by peer", 4) is None
    assert _scheduled_pipeline_retry_delay("invalid database schema", 1) is None
    assert _scheduled_pipeline_retry_delay(None, 1) is None


def test_schedule_view_exposes_the_next_retry_as_utc() -> None:
    job = _pipeline_job()
    job.next_attempt_at = datetime(2026, 8, 18, 17, 15)

    view = ScheduleService._view(job)

    assert view.next_attempt_at is not None
    assert view.next_attempt_at.isoformat() == "2026-08-18T17:15:00+00:00"


class _JobSession:
    def __init__(self, job: ScheduledJob) -> None:
        self.job = job

    async def __aenter__(self) -> _JobSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, _model: object, job_id: int) -> ScheduledJob | None:
        return self.job if self.job.id == job_id else None


class _JobSessionMaker:
    def __init__(self, job: ScheduledJob) -> None:
        self.job = job

    def __call__(self) -> _JobSession:
        return _JobSession(self.job)


class _PipelineRecorder:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.calls: list[tuple[int, list[str], dict[str, object]]] = []

    async def ensure_scheduled(
        self,
        job_id: int,
        stages: list[str],
        params: dict[str, object],
    ) -> int:
        self.calls.append((job_id, stages, params))
        if self.error is not None:
            raise PipelineError(self.error)
        return 73


class _RecordingScheduleService(ScheduleService):
    def __init__(self, job: ScheduledJob, pipeline: _PipelineRecorder) -> None:
        self._session_maker = cast(Any, _JobSessionMaker(job))
        self._pipeline = cast(Any, pipeline)
        self._macros = cast(Any, None)
        self.running: tuple[int, int | None] | None = None
        self.deferred: tuple[int, str] | None = None
        self.failed: tuple[int, str] | None = None

    async def _mark_running(
        self,
        job_id: int,
        *,
        pipeline_run_id: int | None = None,
        macro_run_id: int | None = None,
    ) -> None:
        self.running = (job_id, pipeline_run_id)

    async def _defer(self, job_id: int, reason: str) -> None:
        self.deferred = (job_id, reason)

    async def _fail(self, job_id: int, error: str) -> None:
        self.failed = (job_id, error)


def _pipeline_job() -> ScheduledJob:
    return ScheduledJob(
        id=11,
        target="pipeline",
        state="starting",
        run_at=datetime(2026, 8, 18, 17, 0),
        timezone="Europe/Rome",
        payload={
            "stages": ["evidence", "review2", "index"],
            "limits": {"evidence": 2000},
        },
        attempts=1,
        created_at=datetime(2026, 8, 18, 10, 0),
    )


@pytest.mark.asyncio
async def test_dispatch_uses_exact_persisted_pipeline_snapshot() -> None:
    pipeline = _PipelineRecorder()
    service = _RecordingScheduleService(_pipeline_job(), pipeline)

    await service._dispatch(11)

    assert pipeline.calls == [
        (
            11,
            ["evidence", "review2", "index"],
            {
                "limit": None,
                "limits": {"evidence": 2000},
                "max_pages": None,
                "name": None,
            },
        )
    ]
    assert service.running == (11, 73)
    assert service.deferred is None


@pytest.mark.asyncio
async def test_busy_pipeline_defers_schedule_instead_of_failing() -> None:
    pipeline = _PipelineRecorder("pipeline already running")
    service = _RecordingScheduleService(_pipeline_job(), pipeline)

    await service._dispatch(11)

    assert service.running is None
    assert service.failed is None
    assert service.deferred == (11, "waiting for the active pipeline to finish")
