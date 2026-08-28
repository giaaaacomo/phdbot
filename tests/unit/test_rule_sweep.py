from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from phd_searcher.pipeline.rule_sweep import (
    RULE_SWEEP_VERSION,
    _configured_sweep_limit,
    _should_apply_rejection,
    _should_requeue_obsolete_rejection,
    apply_rule_sweep,
    sweep_decision,
)
from phd_searcher.screening import ScreeningDecision


def _position(**overrides):
    values = {
        "title": "Payment by transfer",
        "url": "https://example.test/payment",
        "description": "Transfer semester fees to the university bank account.",
        "full_description": None,
        "position_type": "other",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_rule_sweep_is_versioned_and_rejects_only_high_precision_pages():
    assert RULE_SWEEP_VERSION == "rules-v13"
    assert sweep_decision(_position()).status == "rejected"
    assert sweep_decision(
        _position(
            title="Quantum sensing research project",
            url="https://example.test/research/project",
            description="Research on quantum memories and optical spectroscopy.",
        )
    ).status == "review"


def test_rule_sweep_uses_inline_description_when_fetch_returned_an_error_payload():
    decision = sweep_decision(
        _position(
            title="Opportunities offered",
            url="https://example.test/open-positions",
            description="Applications are invited for a funded doctoral position.",
            full_description='``` {"error_type": "LanguageFolder"} ```',
            position_type="phd",
        )
    )

    assert decision.status == "eligible"


def test_rule_sweep_preserves_grounded_eligible_against_generic_title_rule():
    position = _position(
        title="Sport scholarships",
        position_type="research_fellowship",
        opportunity_kind="vacancy",
        screening_status="eligible",
        screening_source="llm",
        screening_version="evidence-v17",
        screening_decision="eligible",
        screening_confidence=0.95,
        screening_evidence=(
            '["Sport scholarships", '
            '"Applications for the 2026/2027 academic year are now open."]'
        ),
    )

    assert not _should_apply_rejection(
        position,
        ScreeningDecision("rejected", "non_opportunity_page"),
    )


def test_rule_sweep_still_applies_explicit_closure_to_grounded_eligible():
    position = _position(
        screening_status="eligible",
        screening_source="llm",
        screening_version="evidence-v17",
        screening_decision="eligible",
        screening_confidence=0.95,
        screening_evidence='["Applications are open until 31 August."]',
    )

    assert _should_apply_rejection(
        position,
        ScreeningDecision("rejected", "explicitly_closed_or_unavailable"),
    )


def test_rule_sweep_does_not_protect_an_unsupported_evidence_version_positive():
    position = _position(
        title="Scholarships",
        position_type="research_fellowship",
        opportunity_kind="vacancy",
        screening_status="eligible",
        screening_source="llm",
        screening_version="evidence-v17",
        screening_decision="eligible",
        screening_confidence=0.95,
        screening_evidence='["Scholarships"]',
    )

    assert _should_apply_rejection(
        position,
        ScreeningDecision("rejected", "non_opportunity_page"),
    )


def test_rule_sweep_does_not_protect_unvalidated_or_legacy_positive():
    position = _position(
        screening_status="eligible",
        screening_source="llm",
        screening_version="hybrid-v3",
        screening_decision="eligible",
        screening_confidence=0.95,
        screening_evidence='["Scholarship"]',
    )

    assert _should_apply_rejection(
        position,
        ScreeningDecision("rejected", "navigation_link"),
    )


def test_rule_sweep_reopens_only_obsolete_rejections_no_longer_supported():
    position = _position(
        screening_status="rejected",
        screening_source="rules",
        screening_version="rules-v9",
    )

    assert _should_requeue_obsolete_rejection(
        position,
        ScreeningDecision("review", "ambiguous_candidate"),
    )
    assert not _should_requeue_obsolete_rejection(
        position,
        ScreeningDecision("rejected", "non_opportunity_page"),
    )
    position.screening_version = RULE_SWEEP_VERSION
    assert not _should_requeue_obsolete_rejection(
        position,
        ScreeningDecision("eligible", "vacancy_signal"),
    )


@pytest.mark.parametrize(
    ("params", "stage", "expected"),
    [
        ({"limits": {"review2": 100}}, "review2", 100),
        ({"limit": 25}, "review2", 25),
        ({"limits": {"review2": None}}, "review2", None),
        ({"limit": 25, "limits": {"review2": None}}, "review2", None),
        ({"limits": {"evidence": 2000, "review2": 100}}, "evidence", 2000),
        ({"limits": {"review2": 0}}, "review2", 0),
    ],
)
def test_rule_sweep_uses_the_active_stage_limit(params, stage, expected):
    assert _configured_sweep_limit(params, stage) == expected


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _CapturingSession:
    statement = None

    async def execute(self, statement):
        self.statement = statement
        return _EmptyResult()

    async def get(self, _model, _identity):
        return None

    async def commit(self):
        raise AssertionError("an empty sweep must not commit")


@pytest.mark.asyncio
async def test_rule_sweep_rechecks_legacy_eligible_records():
    session = _CapturingSession()

    assert await apply_rule_sweep(session, pipeline_run_id=None) == 0
    assert session.statement is not None
    expanding_values = [
        value
        for value in session.statement.compile().params.values()
        if isinstance(value, (tuple, list))
    ]
    assert any(set(value) == {"review", "eligible"} for value in expanding_values)


@pytest.mark.asyncio
async def test_rule_sweep_bounds_its_query_to_the_pipeline_stage_limit():
    session = _CapturingSession()
    session.get = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            params={"limits": {"review2": 100}},
            current_stage="review2",
        )
    )

    assert await apply_rule_sweep(session, pipeline_run_id=47) == 0
    assert session.statement is not None
    assert session.statement.compile().params["param_1"] == 100
