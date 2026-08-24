"""Conservative, reversible priors derived from dimensioned operator feedback."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from phd_searcher.pipeline.source_family import (
    FamilyCounts,
    FamilyDirection,
    family_direction,
)


@dataclass(frozen=True, slots=True)
class FamilyFeedbackRow:
    position_id: int
    value: str
    family_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FamilyFeedbackSignal:
    direction: FamilyDirection
    samples: int
    family_key: str


FamilyFeedbackProfiles = dict[str, dict[int, bool | None]]


def build_family_feedback_profiles(
    rows: Iterable[FamilyFeedbackRow],
) -> FamilyFeedbackProfiles:
    """Group distinct item labels; conflicts never become training evidence."""
    profiles: FamilyFeedbackProfiles = {}
    for row in rows:
        if row.value not in {"yes", "no"}:
            continue
        label = row.value == "yes"
        for key in dict.fromkeys(row.family_keys):
            labels = profiles.setdefault(key, {})
            previous = labels.get(row.position_id)
            if previous is not None and previous != label:
                labels[row.position_id] = None
            elif row.position_id not in labels:
                labels[row.position_id] = label
    return profiles


def _counts_excluding(
    labels: dict[int, bool | None],
    position_id: int,
) -> FamilyCounts:
    usable = [label for item_id, label in labels.items() if item_id != position_id and label is not None]
    return FamilyCounts(
        opportunities=sum(label is True for label in usable),
        non_opportunities=sum(label is False for label in usable),
    )


def family_feedback_signal(
    position_id: int,
    family_keys: Iterable[str],
    profiles: FamilyFeedbackProfiles,
) -> FamilyFeedbackSignal | None:
    """Choose the first sufficiently supported key, using leave-one-out counts."""
    for key in family_keys:
        labels = profiles.get(key)
        if not labels:
            continue
        counts = _counts_excluding(labels, position_id)
        is_parent_route = "|route:" in key
        direction = family_direction(
            counts,
            minimum_samples=20 if is_parent_route else 12,
            opportunity_lower_bound=0.80 if is_parent_route else 0.70,
            non_opportunity_upper_bound=0.20 if is_parent_route else 0.30,
        )
        if direction in {
            FamilyDirection.SUPPORTS_OPPORTUNITY,
            FamilyDirection.SUPPORTS_NON_OPPORTUNITY,
        }:
            return FamilyFeedbackSignal(
                direction=direction,
                samples=counts.total,
                family_key=key,
            )
    return None
