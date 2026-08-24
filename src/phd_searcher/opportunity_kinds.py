"""Stable taxonomy for the kind of opportunity represented by a position."""

from __future__ import annotations

from typing import Final, Literal

OpportunityKind = Literal[
    "unknown",
    "vacancy",
    "programme",
    "spontaneous",
    "information",
]

UNKNOWN: Final[OpportunityKind] = "unknown"
VACANCY: Final[OpportunityKind] = "vacancy"
PROGRAMME: Final[OpportunityKind] = "programme"
SPONTANEOUS: Final[OpportunityKind] = "spontaneous"
INFORMATION: Final[OpportunityKind] = "information"

OPPORTUNITY_KINDS: Final[frozenset[OpportunityKind]] = frozenset(
    {UNKNOWN, VACANCY, PROGRAMME, SPONTANEOUS, INFORMATION}
)
DEFAULT_OPPORTUNITY_KIND: Final[OpportunityKind] = UNKNOWN


def normalize_opportunity_kind(value: object) -> OpportunityKind:
    """Return a canonical opportunity kind, falling back to ``unknown``."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in OPPORTUNITY_KINDS:
            return normalized
    return DEFAULT_OPPORTUNITY_KIND
