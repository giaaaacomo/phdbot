from phd_searcher.pipeline.family_feedback import (
    FamilyFeedbackRow,
    build_family_feedback_profiles,
    family_feedback_signal,
)
from phd_searcher.pipeline.source_family import FamilyDirection


def _rows(value: str, count: int, key: str, *, start: int = 1) -> list[FamilyFeedbackRow]:
    return [
        FamilyFeedbackRow(position_id=position_id, value=value, family_keys=(key,))
        for position_id in range(start, start + count)
    ]


def test_specific_family_signal_requires_independent_siblings_and_leave_one_out() -> None:
    key = "listing:7|template:jobs.example.test/vacancies/{id}"
    profiles = build_family_feedback_profiles(_rows("yes", 12, key))

    assert family_feedback_signal(1, (key,), profiles) is None
    signal = family_feedback_signal(99, (key,), profiles)
    assert signal is not None
    assert signal.direction == FamilyDirection.SUPPORTS_OPPORTUNITY
    assert signal.samples == 12


def test_parent_routes_use_a_larger_stricter_sample() -> None:
    key = "listing:7|route:jobs.example.test/vacancies/{item}"
    profiles = build_family_feedback_profiles(_rows("no", 19, key))
    assert family_feedback_signal(99, (key,), profiles) is None

    profiles = build_family_feedback_profiles(_rows("no", 20, key))
    signal = family_feedback_signal(99, (key,), profiles)
    assert signal is not None
    assert signal.direction == FamilyDirection.SUPPORTS_NON_OPPORTUNITY


def test_conflicting_labels_for_one_item_are_excluded_not_double_counted() -> None:
    key = "listing:7|template:jobs.example.test/vacancies/{id}"
    rows = [
        *_rows("yes", 12, key),
        FamilyFeedbackRow(position_id=1, value="no", family_keys=(key, key)),
    ]
    profiles = build_family_feedback_profiles(rows)

    assert profiles[key][1] is None
    assert family_feedback_signal(99, (key,), profiles) is None


def test_non_opportunity_dimensions_never_enter_profiles() -> None:
    key = "listing:7|template:jobs.example.test/vacancies/{id}"
    profiles = build_family_feedback_profiles(
        [FamilyFeedbackRow(position_id=1, value="closed", family_keys=(key,))]
    )
    assert profiles == {}
