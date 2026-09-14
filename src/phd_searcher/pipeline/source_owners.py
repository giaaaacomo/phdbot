"""Resolve member recruitment links against the existing institution catalogue.

No model, network, new catalogue entries or recursive crawling. Shared hosts
and subpaths require an unambiguous most-specific official website match.
"""

from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlsplit


def _parts(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
            return None
        return parsed.hostname.casefold().removeprefix("www."), parsed.path.rstrip("/")
    except ValueError:
        return None


class SourceOwners:
    def __init__(self, catalogue: list[tuple[int, str]]) -> None:
        self.hosts: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for identifier, website in catalogue:
            parts = _parts(website)
            if parts is None:
                continue
            host, path = parts
            # A homepage language selector is not an institution boundary.
            if re.fullmatch(r"/(?:en|it|fr|de|es|pt|nl|sv|fi|da|no)(?:-[A-Z]{2})?", path):
                path = ""
            self.hosts[host].append((path, identifier))

    def resolve(self, url: str) -> int | None:
        parts = _parts(url)
        if parts is None:
            return None
        host, path = parts
        labels = host.split(".")
        matches: dict[tuple[int, int], set[int]] = defaultdict(set)
        for offset in range(len(labels) - 1):
            official_host = ".".join(labels[offset:])
            for prefix, identifier in self.hosts.get(official_host, []):
                # A path-scoped lab cannot claim a sibling subdomain or paths.
                if prefix and (offset or not (path == prefix or path.startswith(prefix + "/"))):
                    continue
                matches[(len(official_host), len(prefix))].add(identifier)
        if not matches:
            return None
        owners = matches[max(matches)]
        return next(iter(owners)) if len(owners) == 1 else None
