"""Low-overhead adaptive supervisor for an already-running PHDBOT pipeline.

The process performs ordinary health checks locally, without an LLM.  It emits
only useful events, keeps a compact state file for later inspection, and can
optionally resume failures whose error text is clearly transient.  It never
starts a new run and never stops a running one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_MIN_INTERVAL_SECONDS = 30
_MAX_INTERVAL_SECONDS = 900
_DEFAULT_INTERVAL_SECONDS = 300
_TRANSIENT_ERROR_MARKERS = (
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


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def adaptive_interval(status: dict[str, Any]) -> int:
    """Choose a sparse cadence, tightening near completion or a deferred retry."""
    if status.get("state") != "running":
        return _MIN_INTERVAL_SECONDS

    stage = status.get("current_stage")
    if not isinstance(stage, dict):
        return _DEFAULT_INTERVAL_SECONDS

    eta = _number(stage.get("eta_seconds"))
    average = _number(stage.get("avg_seconds"))
    # Roughly eight observations over the remaining stage, but never poll
    # more frequently than about four expected work items.
    interval = float(_DEFAULT_INTERVAL_SECONDS) if eta is None or eta <= 0 else eta / 8
    if average is not None and average > 0:
        interval = max(interval, average * 4)

    deferred = stage.get("deferred_queue")
    if isinstance(deferred, dict):
        retry_in = _number(deferred.get("retry_in_seconds"))
        if retry_in is not None and retry_in >= 0:
            interval = min(interval, retry_in + 5)

    return round(max(_MIN_INTERVAL_SECONDS, min(interval, _MAX_INTERVAL_SECONDS)))


def is_transient_failure(error: object) -> bool:
    if not isinstance(error, str):
        return False
    normalized = error.casefold()
    return any(marker in normalized for marker in _TRANSIENT_ERROR_MARKERS)


def _request(url: str, *, payload: dict[str, object] | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    with urlopen(request, timeout=15) as response:
        parsed = json.load(response)
    if not isinstance(parsed, dict):
        raise TypeError("PHDBOT returned a non-object response")
    return parsed


def _stage(status: dict[str, Any]) -> dict[str, Any]:
    value = status.get("current_stage")
    return value if isinstance(value, dict) else {}


def _progress_signature(status: dict[str, Any]) -> tuple[object, ...]:
    stage = _stage(status)
    deferred = stage.get("deferred_queue")
    deferred_signature: tuple[object, ...] = ()
    if isinstance(deferred, dict):
        deferred_signature = (
            deferred.get("processed"),
            deferred.get("remaining"),
            deferred.get("rate_limit_streak"),
        )
    return (
        status.get("run_id"),
        status.get("state"),
        stage.get("name"),
        stage.get("done"),
        stage.get("current"),
        *deferred_signature,
    )


def _progress_bucket(status: dict[str, Any]) -> int | None:
    stage = _stage(status)
    done = _number(stage.get("done"))
    total = _number(stage.get("total"))
    if done is None or total is None or total <= 0:
        return None
    return min(10, int((done / total) * 10))


class EventWriter:
    def __init__(self, event_log: Path | None) -> None:
        self._event_log = event_log

    def emit(self, kind: str, **details: object) -> None:
        event = {
            "at": datetime.now(UTC).isoformat(),
            "kind": kind,
            **details,
        }
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        print(encoded, flush=True)
        if self._event_log is not None:
            self._event_log.parent.mkdir(parents=True, exist_ok=True)
            with self._event_log.open("a", encoding="utf-8") as stream:
                stream.write(f"{encoded}\n")


def _write_state(path: Path | None, status: dict[str, Any], interval: int, resumes: int) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "observed_at": datetime.now(UTC).isoformat(),
        "next_check_seconds": interval,
        "automatic_resumes": resumes,
        "status": status,
    }
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _failure_cooldown(error: object, configured_seconds: int) -> int:
    if isinstance(error, str) and ("429" in error or "rate limit" in error.casefold()):
        return max(configured_seconds, 900)
    return configured_seconds


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8003")
    parser.add_argument("--expected-run", type=int, required=True)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--event-log", type=Path)
    parser.add_argument("--auto-resume-transient", action="store_true")
    parser.add_argument("--max-auto-resumes", type=int, default=3)
    parser.add_argument("--resume-cooldown", type=int, default=300)
    parser.add_argument("--max-runtime", type=int, default=259_200)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    status_url = f"{base_url}/v1/pipeline/status"
    resume_url = f"{base_url}/v1/pipeline/resume"
    writer = EventWriter(args.event_log)
    deadline = time.monotonic() + args.max_runtime
    last_signature: tuple[object, ...] | None = None
    last_progress_at = time.monotonic()
    last_stage: object = None
    last_bucket: int | None = None
    unavailable_count = 0
    resumes = 0
    stall_reported_for: tuple[object, ...] | None = None

    writer.emit("supervisor_started", expected_run=args.expected_run)
    while time.monotonic() < deadline:
        try:
            status = _request(status_url)
            unavailable_count = 0
        except (HTTPError, OSError, URLError, TimeoutError, TypeError, ValueError, json.JSONDecodeError) as exc:
            unavailable_count += 1
            if unavailable_count in {1, 3}:
                writer.emit("status_unavailable", failures=unavailable_count, error=type(exc).__name__)
            time.sleep(_MIN_INTERVAL_SECONDS)
            continue

        run_id = status.get("run_id")
        if run_id != args.expected_run:
            writer.emit("run_changed", expected=args.expected_run, observed=run_id, state=status.get("state"))
            return 2

        stage = _stage(status)
        stage_name = stage.get("name")
        signature = _progress_signature(status)
        now = time.monotonic()
        if signature != last_signature:
            last_signature = signature
            last_progress_at = now
            stall_reported_for = None

        if stage_name != last_stage:
            writer.emit(
                "stage_changed",
                run_id=run_id,
                previous=last_stage,
                stage=stage_name,
                done=stage.get("done"),
                total=stage.get("total"),
            )
            last_stage = stage_name
            last_bucket = _progress_bucket(status)
        else:
            bucket = _progress_bucket(status)
            if bucket is not None and bucket != last_bucket:
                writer.emit(
                    "progress_milestone",
                    run_id=run_id,
                    stage=stage_name,
                    done=stage.get("done"),
                    total=stage.get("total"),
                    percent=bucket * 10,
                )
                last_bucket = bucket

        state = status.get("state")
        interval = adaptive_interval(status)
        _write_state(args.state_file, status, interval, resumes)

        if state == "done":
            writer.emit("run_done", run_id=run_id, stages_done=status.get("stages_done"))
            return 0
        if state == "stopped":
            writer.emit("run_stopped", run_id=run_id)
            return 0
        if state == "failed":
            error = status.get("error")
            if not args.auto_resume_transient or not is_transient_failure(error):
                writer.emit("run_failed", run_id=run_id, transient=False, error=error)
                return 3
            if resumes >= args.max_auto_resumes:
                writer.emit("resume_budget_exhausted", run_id=run_id, resumes=resumes, error=error)
                return 4
            cooldown = _failure_cooldown(error, args.resume_cooldown)
            writer.emit("resume_scheduled", run_id=run_id, cooldown_seconds=cooldown, error=error)
            time.sleep(cooldown)
            latest = _request(status_url)
            if latest.get("run_id") != args.expected_run or latest.get("state") != "failed":
                writer.emit(
                    "resume_cancelled",
                    run_id=run_id,
                    observed_run=latest.get("run_id"),
                    observed_state=latest.get("state"),
                )
                continue
            try:
                resumed = _request(resume_url, payload={})
            except (HTTPError, OSError, URLError, TimeoutError, TypeError, ValueError, json.JSONDecodeError) as exc:
                writer.emit("resume_request_failed", run_id=run_id, error=type(exc).__name__)
                time.sleep(_MIN_INTERVAL_SECONDS)
                continue
            resumes += 1
            writer.emit("run_resumed", run_id=run_id, resumes=resumes, state=resumed.get("state"))
            last_signature = None
            continue

        average = _number(stage.get("avg_seconds"))
        stall_after = max(900.0, (average or 0) * 20)
        if now - last_progress_at >= stall_after and stall_reported_for != signature:
            writer.emit(
                "possible_stall",
                run_id=run_id,
                stage=stage_name,
                unchanged_seconds=round(now - last_progress_at),
                current=stage.get("current"),
            )
            stall_reported_for = signature

        time.sleep(interval)

    writer.emit("supervisor_deadline", expected_run=args.expected_run)
    return 5


if __name__ == "__main__":
    sys.exit(main())
