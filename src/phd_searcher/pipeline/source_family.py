"""Conservative URL-family features for source-aware screening.

The family is a routing prior, never candidate-level evidence.  In particular,
an URL sibling cannot prove that a position is open, closed, eligible or
rejected.  Keeping the implementation pure makes it possible to backtest the
signal without touching production rows.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from urllib.parse import parse_qsl, unquote, urlsplit

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_DATE_SEGMENT_RE = re.compile(r"^(?:19|20)\d{2}(?:[-_/](?:0?[1-9]|1[0-2]))?(?:[-_/](?:0?[1-9]|[12]\d|3[01]))?$")
_LONG_HEX_RE = re.compile(r"^[0-9a-f]{12,}$", re.IGNORECASE)
_LONG_NUMBER_RE = re.compile(r"\d{4,}")
_SPACE_RE = re.compile(r"\s+")
_IDENTIFIER_QUERY_RE = re.compile(
    r"(?:^|[^a-z])(?:id|job|jobs|jobid|job-id|position|posting|requisition|req|vacancy|vacature)(?:$|[^a-z])",
    re.IGNORECASE,
)
_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "source",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)
SOURCE_FAMILY_VERSION = "url-family-v1"
_MAX_FAMILY_KEY_LENGTH = 512


class FamilyDirection(StrEnum):
    """Direction of a statistically bounded family prior."""

    SUPPORTS_OPPORTUNITY = "supports_opportunity"
    SUPPORTS_NON_OPPORTUNITY = "supports_non_opportunity"
    MIXED = "mixed"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class FamilyCounts:
    """Evidence-grounded item labels observed for one URL family."""

    opportunities: int = 0
    non_opportunities: int = 0

    @property
    def total(self) -> int:
        return self.opportunities + self.non_opportunities

    @property
    def opportunity_rate(self) -> float | None:
        return self.opportunities / self.total if self.total else None

    def excluding(self, label: bool) -> FamilyCounts:
        """Return leave-one-out counts for a labelled candidate."""
        if label:
            return FamilyCounts(max(0, self.opportunities - 1), self.non_opportunities)
        return FamilyCounts(self.opportunities, max(0, self.non_opportunities - 1))

    def adding(self, label: bool, count: int = 1) -> FamilyCounts:
        """Return immutable counts with one or more observations added."""
        if count < 0:
            raise ValueError("count must be non-negative")
        if label:
            return FamilyCounts(self.opportunities + count, self.non_opportunities)
        return FamilyCounts(self.opportunities, self.non_opportunities + count)

    def subtracting(self, other: FamilyCounts) -> FamilyCounts:
        """Subtract a known subset, rejecting inconsistent aggregates."""
        if other.opportunities > self.opportunities or other.non_opportunities > self.non_opportunities:
            raise ValueError("cannot subtract counts that are not a subset")
        return FamilyCounts(
            self.opportunities - other.opportunities,
            self.non_opportunities - other.non_opportunities,
        )


def _host(url: str) -> str | None:
    try:
        parsed = urlsplit(url.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        return None
    normalized = hostname.casefold().rstrip(".")
    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    return f"{normalized}:{port}" if port and not default_port else normalized


def _clean_segment(segment: str) -> str:
    value = _SPACE_RE.sub("-", unicodedata.normalize("NFKC", unquote(segment)).strip()).casefold()
    if not value:
        return value
    if value.isdecimal():
        return "{id}"
    if _UUID_RE.fullmatch(value):
        return "{uuid}"
    if _DATE_SEGMENT_RE.fullmatch(value):
        return "{date}"
    if _LONG_HEX_RE.fullmatch(value):
        return "{token}"
    # Common job systems embed their numeric identifier in a readable slug.
    # Four digits avoids erasing ordinary tokens such as ``r2`` or ``h202``.
    return _LONG_NUMBER_RE.sub("{id}", value)


def _path_parts(url: str) -> tuple[str, ...]:
    try:
        return tuple(
            cleaned
            for segment in urlsplit(url.strip()).path.split("/")
            if (cleaned := _clean_segment(segment))
        )
    except ValueError:
        return ()


def _path(parts: tuple[str, ...]) -> str:
    return "/" + "/".join(parts) if parts else "/"


def _identifier_query_keys(url: str) -> tuple[str, ...]:
    try:
        pairs = parse_qsl(urlsplit(url.strip()).query, keep_blank_values=True)
    except ValueError:
        return ()
    keys = {
        unicodedata.normalize("NFKC", key).casefold()
        for key, _value in pairs
        if key.casefold() not in _TRACKING_QUERY_KEYS and _IDENTIFIER_QUERY_RE.search(key)
    }
    return tuple(sorted(keys))


def _bounded_key(value: str) -> str:
    if len(value) <= _MAX_FAMILY_KEY_LENGTH:
        return value
    digest = sha256(value.encode()).hexdigest()[:16]
    return f"{value[: _MAX_FAMILY_KEY_LENGTH - len(digest) - 4]}...:{digest}"


def source_family_keys(
    position_url: str,
    *,
    listing_url: str | None = None,
    listing_page_id: int | None = None,
) -> tuple[str, ...]:
    """Return conservative family keys ordered from specific to broad.

    ``listing_page_id`` intentionally scopes families to one extraction source;
    identical paths on two tenants or independently generated schemas must not
    share reputation.  The broader route key is emitted only below a real path
    prefix, so unrelated root pages are never collapsed into one family.
    """
    host = _host(position_url)
    if host is None:
        return ()
    parts = _path_parts(position_url)
    try:
        fragment = bool(urlsplit(position_url.strip()).fragment)
    except ValueError:
        fragment = False
    query_keys = _identifier_query_keys(position_url)
    scope = f"listing:{listing_page_id}" if listing_page_id is not None else f"host:{host}"
    suffix = "#item" if fragment else ""
    if query_keys:
        suffix += "?" + "&".join(f"{key}={{value}}" for key in query_keys)

    keys: list[str] = [_bounded_key(f"{scope}|template:{host}{_path(parts)}{suffix}")]

    # Human-readable slugs often contain no machine ID.  Their parent route is
    # a useful, weaker family (for example /jobs/<title>) when it has a genuine
    # namespace.  A one-segment root path is deliberately not generalized.
    if len(parts) >= 2 and parts[-1] not in {"{id}", "{uuid}", "{date}", "{token}"}:
        route = f"{scope}|route:{host}{_path((*parts[:-1], '{item}'))}"
        if query_keys:
            route += "?" + "&".join(f"{key}={{value}}" for key in query_keys)
        keys.append(_bounded_key(route))

    # Inline opportunities use a stable synthetic fragment on the listing
    # page.  Verify the relationship before adding this weaker source key.
    if fragment and listing_url and _host(listing_url) == host:
        listing_parts = _path_parts(listing_url)
        if parts == listing_parts:
            keys.append(_bounded_key(f"{scope}|inline:{host}{_path(parts)}#item"))

    return tuple(dict.fromkeys(keys))


def source_family_audit(position_url: str, listing_page_id: int | None) -> dict[str, object]:
    """Return derivable, versioned audit metadata without claiming a verdict."""
    return {
        "source_family_version": SOURCE_FAMILY_VERSION,
        "source_family_keys": list(
            source_family_keys(position_url, listing_page_id=listing_page_id)
        ),
    }


def wilson_interval(successes: int, total: int, *, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval without external statistical dependencies."""
    if successes < 0 or total < 0 or successes > total:
        raise ValueError("successes must be between zero and total")
    if total == 0:
        return 0.0, 1.0
    proportion = successes / total
    z_squared = z * z
    denominator = 1 + z_squared / total
    centre = proportion + z_squared / (2 * total)
    margin = z * math.sqrt((proportion * (1 - proportion) + z_squared / (4 * total)) / total)
    return (centre - margin) / denominator, (centre + margin) / denominator


def family_direction(
    counts: FamilyCounts,
    *,
    minimum_samples: int = 12,
    opportunity_lower_bound: float = 0.70,
    non_opportunity_upper_bound: float = 0.30,
) -> FamilyDirection:
    """Classify a family using confidence bounds rather than raw purity."""
    if counts.total < minimum_samples:
        return FamilyDirection.INSUFFICIENT
    lower, upper = wilson_interval(counts.opportunities, counts.total)
    if lower >= opportunity_lower_bound:
        return FamilyDirection.SUPPORTS_OPPORTUNITY
    if upper <= non_opportunity_upper_bound:
        return FamilyDirection.SUPPORTS_NON_OPPORTUNITY
    return FamilyDirection.MIXED
