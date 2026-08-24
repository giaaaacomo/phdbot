"""Start a bounded follow-up pipeline only after one specific run succeeds."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from urllib.error import URLError
from urllib.request import Request, urlopen


def _request(url: str, *, payload: dict[str, object] | None = None) -> dict[str, object]:
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


def _log(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat()} {message}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8003")
    parser.add_argument("--after-run", type=int, required=True)
    parser.add_argument("--evidence-limit", type=int)
    parser.add_argument("--review-limit", type=int, default=500)
    parser.add_argument("--index-limit", type=int, default=500)
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--max-wait", type=int, default=21_600)
    args = parser.parse_args()

    deadline = time.monotonic() + args.max_wait
    status_url = f"{args.base_url.rstrip('/')}/v1/pipeline/status"
    start_url = f"{args.base_url.rstrip('/')}/v1/pipeline/start"
    while time.monotonic() < deadline:
        try:
            status = _request(status_url)
        except (OSError, URLError, TimeoutError, TypeError, ValueError, json.JSONDecodeError) as exc:
            _log(f"status unavailable ({type(exc).__name__}); retrying")
            time.sleep(args.interval)
            continue

        run_id = status.get("run_id")
        state = status.get("state")
        if run_id != args.after_run:
            _log(f"abort: expected run {args.after_run}, found run {run_id}")
            return 2
        if state == "done":
            stages = ["review2", "index"]
            limits = {
                "review2": args.review_limit,
                "index": args.index_limit,
            }
            if args.evidence_limit is not None:
                stages.insert(0, "evidence")
                limits["evidence"] = args.evidence_limit
            follow_up = _request(
                start_url,
                payload={
                    "stages": stages,
                    "limits": limits,
                },
            )
            _log(f"started follow-up run {follow_up.get('run_id')}")
            return 0
        if state in {"failed", "stopped", "idle"}:
            _log(f"abort: run {args.after_run} ended as {state}")
            return 3
        time.sleep(args.interval)

    _log(f"abort: run {args.after_run} did not finish before max-wait")
    return 4


if __name__ == "__main__":
    sys.exit(main())
