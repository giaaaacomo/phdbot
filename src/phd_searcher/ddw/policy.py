"""Bounded robots check using the worker's public-address-only transport."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from phd_searcher.ddw import worker

AGENT = "PHDBOT-DDW-experiment/1"


def _observe(url: str) -> dict[str, Any]:
    host = urlsplit(url).hostname or ""
    worker.public_url(url, [host])
    robots_url = f"https://{host}/robots.txt"
    started = time.perf_counter()
    try:
        raw, _ = worker.fetch_public(robots_url, [host], 10, max_bytes=64_000, content_type="text/plain")
        parser = RobotFileParser()
        parser.parse(raw.decode("utf-8-sig", errors="replace").splitlines())
        return {
            "robots_url": robots_url,
            "observed_at": worker.now(),
            "allowed": parser.can_fetch(AGENT, url),
            "sha256": worker.digest(raw),
            "bytes": len(raw),
            "seconds": time.perf_counter() - started,
        }
    except (OSError, ValueError) as exc:
        return {
            "robots_url": robots_url,
            "observed_at": worker.now(),
            "allowed": False,
            "error_type": type(exc).__name__,
            "seconds": time.perf_counter() - started,
        }


def policy(url: str) -> dict[str, Any]:
    """Bound DNS/TLS/body together; an unresponsive policy cannot outlive the lease."""
    env = {"PYTHONPATH": str(Path(__file__).parents[2]), "PYTHONIOENCODING": "utf-8"}
    for name in ("PATH", "SYSTEMROOT"):
        if name in os.environ:
            env[name] = os.environ[name]
    try:
        result = subprocess.run(
            [sys.executable, "-m", "phd_searcher.ddw.policy", url], env=env, capture_output=True, timeout=12, check=True
        )
        return cast(dict[str, Any], json.loads(result.stdout))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"allowed": False, "observed_at": worker.now(), "error_type": type(exc).__name__}


if __name__ == "__main__":
    print(json.dumps(_observe(sys.argv[1])))
