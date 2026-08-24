#!/usr/bin/env python3
"""Token-free completion guard for a persistent PHDBOT scheduled pipeline.

The supervisor waits for one database-backed schedule, records a compact
catalogue snapshot and can deploy an already-built API image plus launch one
index-only reconciliation.  It never interrupts a running pipeline and it
does not invoke Codex or an LLM.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ScheduleAction = Literal["wait", "deploy_index", "report_only", "abort"]
_ACTIVE_SCHEDULE_STATES = {"scheduled", "starting", "waiting_pipeline", "running"}
_TRANSIENT_MARKERS = (
    "429",
    "bad gateway",
    "connection refused",
    "connection reset",
    "gateway timeout",
    "ollama",
    "rate limit",
    "retries exhausted",
    "server disconnected",
    "service unavailable",
    "temporarily unavailable",
    "timed out",
    "timeout",
)


def completion_action(
    schedule: dict[str, Any],
    pipeline: dict[str, Any],
    *,
    expected_run: int | None,
) -> ScheduleAction:
    """Choose the only safe unattended transition for the observed state."""
    pipeline_run_id = schedule.get("pipeline_run_id")
    if (
        expected_run is not None
        and pipeline_run_id is not None
        and pipeline_run_id != expected_run
    ):
        return "abort"
    state = schedule.get("state")
    if state in _ACTIVE_SCHEDULE_STATES:
        return "wait"
    if state == "done":
        return "deploy_index"
    if state == "failed":
        # Respect an intentional checkpoint-preserving Stop. Other terminal
        # failures may still leave useful evidence that should become visible.
        if pipeline.get("run_id") == pipeline_run_id and pipeline.get("state") == "stopped":
            return "report_only"
        return "deploy_index"
    if state in {"cancelled", "stopped"}:
        return "report_only"
    return "abort"


def is_transient_failure(error: object) -> bool:
    return isinstance(error, str) and any(
        marker in error.casefold() for marker in _TRANSIENT_MARKERS
    )


def _request(url: str, *, payload: dict[str, object] | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    with urlopen(request, timeout=20) as response:
        parsed = json.load(response)
    if not isinstance(parsed, dict):
        raise TypeError("PHDBOT returned a non-object response")
    return parsed


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _saved_followup_run(state: dict[str, Any]) -> int | None:
    direct = state.get("followup_run")
    if isinstance(direct, int):
        return direct
    status = state.get("followup_status")
    nested = status.get("run_id") if isinstance(status, dict) else None
    return nested if isinstance(nested, int) else None


class Journal:
    def __init__(self, event_log: Path) -> None:
        self._event_log = event_log

    def emit(self, kind: str, **details: object) -> None:
        event = {"at": datetime.now(UTC).isoformat(), "kind": kind, **details}
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        print(encoded, flush=True)
        self._event_log.parent.mkdir(parents=True, exist_ok=True)
        with self._event_log.open("a", encoding="utf-8") as stream:
            stream.write(f"{encoded}\n")


def _state(
    path: Path,
    *,
    phase: str,
    schedule_id: int,
    expected_run: int | None,
    **details: object,
) -> None:
    _atomic_json(
        path,
        {
            "updated_at": datetime.now(UTC).isoformat(),
            "phase": phase,
            "schedule_id": schedule_id,
            "expected_run": expected_run,
            **details,
        },
    )


def _catalog_snapshot(base_url: str, schedule_id: int) -> dict[str, object]:
    schedule = _request(f"{base_url}/v1/schedules/{schedule_id}")
    pipeline = _request(f"{base_url}/v1/pipeline/status")
    screening = _request(f"{base_url}/v1/screening?limit=1")
    coverage = _request(f"{base_url}/v1/universities")
    qdrant = _request("http://127.0.0.1:6333/collections/positions")
    universities = coverage.get("universities")
    rows = universities if isinstance(universities, list) else []
    qdrant_result = qdrant.get("result")
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "schedule": schedule,
        "pipeline": pipeline,
        "screening_counts": screening.get("counts", {}),
        "active_screening_total": screening.get("total", 0),
        "coverage": {
            "institutions": len(rows),
            "with_positions": sum(
                1
                for row in rows
                if isinstance(row, dict) and int(row.get("positions_count", 0)) > 0
            ),
            "positions": sum(
                int(row.get("positions_count", 0))
                for row in rows
                if isinstance(row, dict)
            ),
            "listing_pages": sum(
                int(row.get("listing_pages_count", 0))
                for row in rows
                if isinstance(row, dict)
            ),
        },
        "qdrant_points": (
            qdrant_result.get("points_count")
            if isinstance(qdrant_result, dict)
            else None
        ),
    }


def _deploy_api(project_dir: Path) -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "api",
        ],
        cwd=project_dir,
        check=True,
        timeout=600,
    )


def _wait_for_api(base_url: str, *, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            _request(f"{base_url}/health")
            return
        except (HTTPError, OSError, URLError, TimeoutError, TypeError, ValueError):
            time.sleep(10)
    raise TimeoutError("PHDBOT API did not become healthy after deployment")


def _publish_followup(
    base_url: str,
    *,
    expected_run: int,
    followup_stages: list[str],
    followup_limits: dict[str, int | None],
    poll_seconds: int,
    journal: Journal,
    state_file: Path,
    schedule_id: int,
    initial_resumes: int = 0,
) -> dict[str, Any]:
    status_url = f"{base_url}/v1/pipeline/status"
    status = _request(status_url)
    run_id = status.get("run_id")
    stages = status.get("stages")
    if (
        isinstance(run_id, int)
        and run_id > expected_run
        and stages == followup_stages
        and status.get("state") in {"running", "failed", "done"}
    ):
        followup_run = run_id
        journal.emit("publish_followup_adopted", run_id=followup_run, stages=followup_stages)
    else:
        while status.get("state") in {"running", "stopping"}:
            time.sleep(poll_seconds)
            status = _request(status_url)
        started = _request(
            f"{base_url}/v1/pipeline/start",
            payload={"stages": followup_stages, "limits": followup_limits},
        )
        followup_run = int(started["run_id"])
        journal.emit("publish_followup_started", run_id=followup_run, stages=followup_stages)

    # Persist the identity before the first poll. If systemd restarts us in
    # this narrow window, the next process adopts this run instead of
    # redeploying the API or starting a duplicate reconciliation.
    _state(
        state_file,
        phase="followup",
        schedule_id=schedule_id,
        expected_run=expected_run,
        followup_run=followup_run,
        followup_stages=followup_stages,
        followup_limits=followup_limits,
        automatic_resumes=initial_resumes,
    )

    resumes = initial_resumes
    while True:
        status = _request(status_url)
        if status.get("run_id") != followup_run:
            raise RuntimeError(
                f"publish follow-up changed from run {followup_run} to {status.get('run_id')}"
            )
        _state(
            state_file,
            phase="followup",
            schedule_id=schedule_id,
            expected_run=expected_run,
            followup_run=followup_run,
            followup_stages=followup_stages,
            followup_limits=followup_limits,
            followup_status=status,
            automatic_resumes=resumes,
        )
        state = status.get("state")
        if state == "done":
            journal.emit("publish_followup_done", run_id=followup_run)
            return status
        if state == "failed":
            error = status.get("error")
            if resumes >= 3 or not is_transient_failure(error):
                raise RuntimeError(f"publish follow-up failed: {error}")
            cooldown = 900 if isinstance(error, str) and "429" in error else 300
            journal.emit(
                "publish_followup_resume_scheduled",
                run_id=followup_run,
                cooldown_seconds=cooldown,
                error=error,
            )
            time.sleep(cooldown)
            resumes += 1
            _state(
                state_file,
                phase="followup",
                schedule_id=schedule_id,
                expected_run=expected_run,
                followup_run=followup_run,
                followup_stages=followup_stages,
                followup_limits=followup_limits,
                followup_status=status,
                automatic_resumes=resumes,
            )
            _request(f"{base_url}/v1/pipeline/resume", payload={})
        elif state == "stopped":
            raise RuntimeError("publish follow-up was deliberately stopped")
        time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8003")
    parser.add_argument("--schedule-id", type=int, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=int, default=300)
    parser.add_argument("--max-runtime", type=int, default=259_200)
    parser.add_argument("--deploy-and-index", action="store_true")
    parser.add_argument(
        "--enrich-limit",
        type=int,
        default=0,
        help="bounded eligible detail pages to enrich before the final index",
    )
    args = parser.parse_args()
    if args.enrich_limit < 0:
        parser.error("--enrich-limit must be zero or positive")

    base_url = args.base_url.rstrip("/")
    journal = Journal(args.event_log)
    deadline = time.monotonic() + args.max_runtime
    expected_run: int | None = None
    saved_state = _read_json(args.state_file)
    followup_stages = ["enrich", "index"] if args.enrich_limit else ["index"]
    followup_limits: dict[str, int | None] = {"index": None}
    if args.enrich_limit:
        followup_limits["enrich"] = args.enrich_limit
    journal.emit("completion_supervisor_started", schedule_id=args.schedule_id)

    while time.monotonic() < deadline:
        try:
            schedule = _request(f"{base_url}/v1/schedules/{args.schedule_id}")
            pipeline = _request(f"{base_url}/v1/pipeline/status")
        except (HTTPError, OSError, URLError, TimeoutError, TypeError, ValueError) as exc:
            journal.emit("status_unavailable", error=type(exc).__name__)
            time.sleep(args.poll_seconds)
            continue

        observed_run = schedule.get("pipeline_run_id")
        if expected_run is None and isinstance(observed_run, int):
            expected_run = observed_run
            journal.emit("scheduled_run_locked", run_id=expected_run)
        action = completion_action(schedule, pipeline, expected_run=expected_run)
        if (
            action != "wait"
            and saved_state.get("phase") == "done"
            and saved_state.get("schedule_id") == args.schedule_id
            and saved_state.get("expected_run") == expected_run
        ):
            # Preserve the terminal marker rather than replacing it with the
            # latest observation, so repeated manual starts remain idempotent.
            _atomic_json(args.state_file, saved_state)
            journal.emit("completion_already_done", expected_run=expected_run)
            return 0
        _state(
            args.state_file,
            phase="waiting" if action == "wait" else "terminal_observed",
            schedule_id=args.schedule_id,
            expected_run=expected_run,
            schedule=schedule,
            pipeline=pipeline,
            action=action,
        )
        if action == "wait":
            time.sleep(args.poll_seconds)
            continue
        if action == "abort":
            journal.emit("completion_aborted", schedule=schedule, pipeline=pipeline)
            return 2

        saved_followup_run = _saved_followup_run(saved_state)
        current_run = pipeline.get("run_id")
        current_state = pipeline.get("state")
        adopting_followup = (
            saved_followup_run is not None
            and current_run == saved_followup_run
            and pipeline.get("stages") == followup_stages
        )
        if (
            isinstance(current_run, int)
            and expected_run is not None
            and current_run > expected_run
            and not adopting_followup
        ):
            # A person or another scheduler launched newer work. Never infer
            # ownership of it and never recreate the API underneath it.
            _state(
                args.state_file,
                phase="newer_run_observed",
                schedule_id=args.schedule_id,
                expected_run=expected_run,
                schedule=schedule,
                pipeline=pipeline,
            )
            journal.emit(
                "completion_reported_newer_run",
                expected_run=expected_run,
                observed_run=current_run,
            )
            return 0
        if current_state in {"running", "stopping"} and not adopting_followup:
            # A terminal scheduler row can briefly race with pipeline status.
            # Waiting is always safer than interrupting an active process.
            _state(
                args.state_file,
                phase="waiting_for_idle",
                schedule_id=args.schedule_id,
                expected_run=expected_run,
                schedule=schedule,
                pipeline=pipeline,
            )
            time.sleep(args.poll_seconds)
            continue

        before = _catalog_snapshot(base_url, args.schedule_id)
        if action == "report_only" or not args.deploy_and_index:
            _state(
                args.state_file,
                phase="done",
                schedule_id=args.schedule_id,
                expected_run=expected_run,
                snapshot=before,
                action=action,
            )
            journal.emit("completion_reported", action=action)
            return 0

        if expected_run is None:
            journal.emit("completion_aborted", error="schedule never exposed a pipeline run")
            return 2
        _state(
            args.state_file,
            phase="deploying",
            schedule_id=args.schedule_id,
            expected_run=expected_run,
            snapshot_before=before,
            followup_run=saved_followup_run if adopting_followup else None,
            followup_stages=followup_stages,
            followup_limits=followup_limits,
        )
        if adopting_followup:
            journal.emit(
                "api_deploy_skipped_followup_adopted",
                expected_run=expected_run,
                followup_run=saved_followup_run,
            )
        else:
            journal.emit("api_deploy_started", expected_run=expected_run)
            _deploy_api(args.project_dir.resolve())
            _wait_for_api(base_url, deadline=time.monotonic() + 300)
            journal.emit("api_deploy_done", expected_run=expected_run)
        raw_resumes = saved_state.get("automatic_resumes", 0)
        initial_resumes = raw_resumes if isinstance(raw_resumes, int) else 0
        try:
            followup = _publish_followup(
                base_url,
                expected_run=expected_run,
                followup_stages=followup_stages,
                followup_limits=followup_limits,
                poll_seconds=args.poll_seconds,
                journal=journal,
                state_file=args.state_file,
                schedule_id=args.schedule_id,
                initial_resumes=initial_resumes,
            )
        except RuntimeError as exc:
            latest_state = _read_json(args.state_file)
            _state(
                args.state_file,
                phase="failed",
                schedule_id=args.schedule_id,
                expected_run=expected_run,
                followup_run=_saved_followup_run(latest_state),
                followup_stages=followup_stages,
                followup_limits=followup_limits,
                error=str(exc),
            )
            journal.emit("publish_followup_failed", error=str(exc))
            return 0
        after = _catalog_snapshot(base_url, args.schedule_id)
        _state(
            args.state_file,
            phase="done",
            schedule_id=args.schedule_id,
            expected_run=expected_run,
            snapshot_before=before,
            snapshot_after=after,
            followup=followup,
        )
        journal.emit(
            "completion_done",
            expected_run=expected_run,
            followup_run=followup.get("run_id"),
            qdrant_before=before.get("qdrant_points"),
            qdrant_after=after.get("qdrant_points"),
        )
        return 0

    journal.emit("completion_supervisor_deadline", schedule_id=args.schedule_id)
    return 5


if __name__ == "__main__":
    sys.exit(main())
