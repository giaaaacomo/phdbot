"""Conservative detection for legacy whole-page detail captures.

The detector never mutates stored evidence.  It only decides whether a detail
page is worth fetching again with the current, attributable extractor.
"""

from __future__ import annotations

import re

DETAIL_CLEANUP_VERSION = "attributable-v1"

_MARKDOWN_ASSET = re.compile(r"!\[[^\]]*\]\([^\n)]{1,2000}\)")
_CHROME_PHRASES = (
    "skip to content",
    "skip to main content",
    "accept all cookies",
    "accept cookies",
    "cookie settings",
    "privacy settings",
    "manage cookies",
)


def detail_needs_cleanup(text: str | None) -> bool:
    """Return true only for strong whole-page/chrome signals.

    False positives are deliberately cheap and reversible: a true result only
    queues a source refetch, while the existing text remains available until a
    replacement has been fetched and parsed successfully.
    """

    if not text:
        return False
    folded = " ".join(text.casefold().split())
    return bool(_MARKDOWN_ASSET.search(text)) or any(phrase in folded for phrase in _CHROME_PHRASES)


def detail_refresh_needed(text: str | None, cleanup_version: str | None) -> bool:
    """Missing details and legacy noisy captures both merit one durable fetch."""

    return text is None or (
        cleanup_version != DETAIL_CLEANUP_VERSION and detail_needs_cleanup(text)
    )
