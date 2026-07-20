#!/usr/bin/env python
"""Container healthcheck: probe /health without curl. Exit 0 on HTTP 200."""

import os
import sys
import urllib.request
from urllib.parse import urlparse

URL = os.environ.get("HEALTHCHECK_URL", "http://localhost:8000/health")
TIMEOUT = float(os.environ.get("HEALTHCHECK_TIMEOUT", "5"))
_ALLOWED_SCHEMES = ("http", "https")


def main() -> int:
    if urlparse(URL).scheme not in _ALLOWED_SCHEMES:
        print(f"healthcheck failed: refusing non-HTTP(S) URL {URL!r}", file=sys.stderr)
        return 1
    try:
        with urllib.request.urlopen(URL, timeout=TIMEOUT) as resp:  # nosemgrep: bandit.B310-1
            return 0 if resp.status == 200 else 1
    except Exception as exc:
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
