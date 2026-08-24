import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from phd_searcher.pipeline.auto_review import (
    _COMPATIBLE_REVIEW_VERSIONS,
    _REVIEW_TOOL,
    REVIEW_VERSION,
    AutomaticDecision,
    _accepted_status,
    _apply_review_batch,
    _canonical_position_type,
    _deterministic_opportunity_kind,
    _IncompleteAutomaticReviewError,
    _native_ollama_review,
    _needs_evidence_bypass,
    _parse_tool_call,
    _parse_tool_call_partial,
    _prompt,
    _resilient_ollama_review,
    _review_state,
    _routed_status,
)


def _call(reviews):
    return {"function": {"arguments": json.dumps({"reviews": reviews})}}


def _review_payload(
    position_id,
    *,
    decision="eligible",
    position_type="phd",
    opportunity_kind="vacancy",
    confidence=0.96,
    evidence=None,
    **extra,
):
    return {
        "position_id": position_id,
        "decision": decision,
        "position_type": position_type,
        "opportunity_kind": opportunity_kind,
        "confidence": confidence,
        "evidence": evidence or ["Applications are invited for a PhD position"],
        **extra,
    }


class _FakeToolCall:
    def __init__(self, reviews, call_id="call-1"):
        self.id = call_id
        self.function = SimpleNamespace(arguments=json.dumps({"reviews": reviews}))

    def model_dump(self, **_kwargs):
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": "submit_position_reviews",
                "arguments": self.function.arguments,
            },
        }


def _completion(call):
    message = SimpleNamespace(content="", tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _position(position_id):
    return SimpleNamespace(
        id=position_id,
        title=f"Position {position_id}",
        url=f"https://example.test/{position_id}",
        institution_name="Example University",
        institution_country="IT",
        position_type="other",
        opportunity_kind="unknown",
        full_description=None,
        description="Applications are invited for a PhD position. This call is closed.",
    )


def test_review_tool_uses_real_enums_and_a_minimal_contract():
    item_schema = _REVIEW_TOOL["function"]["parameters"]["properties"]["reviews"]["items"]

    assert item_schema["properties"]["decision"]["enum"] == ["eligible", "rejected", "review"]
    assert set(item_schema["properties"]["position_type"]["enum"])
    assert set(item_schema["properties"]["opportunity_kind"]["enum"]) == {
        "unknown",
        "vacancy",
        "programme",
        "spontaneous",
        "information",
    }
    assert item_schema["required"] == [
        "position_id",
        "decision",
        "position_type",
        "opportunity_kind",
        "confidence",
        "evidence",
    ]
    assert "reason" not in item_schema["properties"]


def test_review_v5_preserves_completed_v3_and_v4_llm_work():
    assert REVIEW_VERSION == "hybrid-v5"
    assert _COMPATIBLE_REVIEW_VERSIONS == ("hybrid-v3", "hybrid-v4", "hybrid-v5")


def test_review_tool_requires_exact_batch_ids():
    call = _call([_review_payload(10)])
    parsed = _parse_tool_call(call, {10})
    assert parsed[0].position_id == 10
    assert parsed[0].reason == "eligible: Applications are invited for a PhD position"

    with pytest.raises(ValueError, match="missing"):
        _parse_tool_call(call, {10, 11})


def test_review_tool_requires_opportunity_kind_without_inventing_a_default():
    payload = _review_payload(10)
    payload.pop("opportunity_kind")

    with pytest.raises(ValueError, match=r"opportunity_kind.*Field required"):
        _parse_tool_call(_call([payload]), {10}, repair_missing_fields=True)


def test_review_prompt_defines_every_opportunity_kind_and_generic_application_guard():
    prompt = _prompt([(_position(10), None)])

    for kind in ("vacancy", "programme", "spontaneous", "information", "unknown"):
        assert kind in prompt
    assert "How to apply" in prompt
    assert "Eligible is allowed only for vacancy, programme or spontaneous" in prompt


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("eligible", "vacancy_signal", "vacancy"),
        ("eligible", "recognized_type:phd", "vacancy"),
        ("review", "recognized_type_without_vacancy_signal:phd", "unknown"),
        ("rejected", "explicitly_closed_or_unavailable", "unknown"),
        ("eligible", "unexpected_positive_rule", "unknown"),
    ],
)
def test_deterministic_kind_is_vacancy_only_for_positive_vacancy_rules(status, reason, expected):
    assert _deterministic_opportunity_kind(status, reason) == expected


def test_review_tool_normalizes_known_human_type_labels():
    assert _canonical_position_type("Master Thesis") == "masters_mph"
    assert _canonical_position_type("Research Assistant") == "assistantship"
    assert _canonical_position_type("Research Internship") == "internship"
    assert _canonical_position_type("non-vacancy") == "other"
    assert _canonical_position_type("not applicable") == "other"
    assert _canonical_position_type("vacancy") == "other"
    with pytest.raises(ValueError, match="allowed canonical"):
        _canonical_position_type("totally invented category")
    assert _canonical_position_type("totally invented category", unknown_as_other=True) == "other"


def test_review_tool_can_coerce_only_unknown_type_after_feedback_budget():
    call = _call(
        [
            {
                "position_id": 10,
                "decision": "rejected",
                "position_type": "invented category",
                "opportunity_kind": "information",
                "confidence": 0.99,
                "reason": "not a vacancy",
                "evidence": ["course information"],
            }
        ]
    )
    with pytest.raises(ValueError, match="allowed canonical"):
        _parse_tool_call(call, {10})
    parsed = _parse_tool_call(call, {10}, unknown_types_as_other=True)
    assert parsed[0].position_type == "other"


def test_review_tool_repairs_omitted_technical_fields_only_on_final_fallback():
    call = _call(
        [
            {
                "decision": "eligible",
                "opportunity_kind": "vacancy",
                "confidence": 0.93,
                "evidence": ["Applications are invited for a doctoral position"],
            },
            {
                "decision": "rejected",
                "opportunity_kind": "information",
                "confidence": 0.99,
                "evidence": ["Read about our completed research project"],
            },
        ]
    )

    with pytest.raises(ValueError, match="Field required"):
        _parse_tool_call(call, [10, 11])

    parsed = _parse_tool_call(call, [10, 11], repair_missing_fields=True)
    assert [item.position_id for item in parsed] == [10, 11]
    assert [item.position_type for item in parsed] == ["other", "other"]
    assert parsed[0].reason.startswith("eligible:")
    assert parsed[1].reason.startswith("rejected:")


def test_review_tool_normalizes_common_model_key_and_value_aliases():
    call = _call(
        [
            {
                "id": 10,
                "verdict": "accept",
                "category": "doctoral position",
                "kind": "vacancy",
                "confidence": 0.98,
                "rationale": "explicit opening",
                "quote": "Applications are invited for a PhD position",
            },
            {
                "candidate_id": 11,
                "decision_type": "reject",
                "opportunity_type": "non-vacancy",
                "opportunity_scope": "information",
                "confidence": 0.99,
                "explanation": "news page",
                "quotes": ["The event took place last year"],
            },
        ]
    )

    parsed = _parse_tool_call(call, [10, 11], repair_missing_fields=True)
    assert [item.decision for item in parsed] == ["eligible", "rejected"]
    assert [item.position_type for item in parsed] == ["phd", "other"]
    assert parsed[0].evidence == ["Applications are invited for a PhD position"]


def test_review_tool_never_repairs_a_partial_or_wrong_batch():
    partial_ids = _call(
        [
            {
                "position_id": 10,
                "decision": "eligible",
                "opportunity_kind": "vacancy",
                "confidence": 0.9,
                "evidence": ["PhD"],
            },
            {
                "decision": "review",
                "opportunity_kind": "unknown",
                "confidence": 0.5,
                "evidence": ["unclear"],
            },
        ]
    )
    wrong_count = _call(
        [
            {
                "decision": "eligible",
                "opportunity_kind": "vacancy",
                "confidence": 0.9,
                "evidence": ["PhD"],
            }
        ]
    )

    with pytest.raises(ValueError, match="position_id"):
        _parse_tool_call(partial_ids, [10, 11], repair_missing_fields=True)
    with pytest.raises(ValueError, match="position_id"):
        _parse_tool_call(wrong_count, [10, 11], repair_missing_fields=True)


def test_review_tool_retains_valid_items_and_retry_is_idempotent():
    first = _parse_tool_call_partial(
        _call(
            [
                {
                    "position_id": 10,
                    "decision": "eligible",
                    "position_type": "phd",
                    "opportunity_kind": "vacancy",
                    "confidence": 0.96,
                    "evidence": ["PhD position"],
                },
                {
                    "position_id": 11,
                    "decision": "rejected",
                    "position_type": "other",
                    "opportunity_kind": "information",
                    "confidence": 0.99,
                },
            ]
        ),
        [10, 11],
    )

    assert [item.position_id for item in first.decisions] == [10]
    assert "position_id=11" in "; ".join(first.errors)

    retry = _parse_tool_call_partial(
        _call(
            [
                # A malformed replay cannot invalidate or replace ID 10.
                {"position_id": 10, "decision": "wrong", "opportunity_kind": "unknown"},
                {
                    "position_id": 11,
                    "decision": "rejected",
                    "position_type": "other",
                    "opportunity_kind": "vacancy",
                    "confidence": 0.99,
                    "evidence": ["This call is closed"],
                },
            ]
        ),
        [11],
        accepted_ids={10},
    )

    assert [item.position_id for item in retry.decisions] == [11]
    assert retry.errors == ()


def test_review_tool_rejects_non_verbatim_evidence_when_context_is_available():
    parsed = _parse_tool_call_partial(
        _call(
            [
                {
                    "position_id": 10,
                    "decision": "eligible",
                    "position_type": "phd",
                    "opportunity_kind": "vacancy",
                    "confidence": 0.99,
                    "evidence": ["Applications close tomorrow"],
                }
            ]
        ),
        [10],
        evidence_contexts={10: "Applications are invited for a doctoral position."},
    )

    assert parsed.decisions == ()
    assert "not verbatim" in "; ".join(parsed.errors)


def test_review_tool_feedback_rejects_semantically_unsupported_eligible_kind():
    evidence = "Applications are open for the PhD programme"
    parsed = _parse_tool_call_partial(
        _call(
            [
                _review_payload(
                    10,
                    opportunity_kind="vacancy",
                    evidence=[evidence],
                )
            ]
        ),
        [10],
        evidence_contexts={10: evidence},
    )

    assert parsed.decisions == ()
    assert "opportunity_kind=vacancy" in "; ".join(parsed.errors)

    corrected = _parse_tool_call_partial(
        _call(
            [
                _review_payload(
                    10,
                    opportunity_kind="programme",
                    evidence=[evidence],
                )
            ]
        ),
        [10],
        evidence_contexts={10: evidence},
    )
    assert [decision.opportunity_kind for decision in corrected.decisions] == ["programme"]
    assert corrected.errors == ()


@pytest.mark.asyncio
async def test_native_review_retries_only_invalid_ids(monkeypatch):
    first_call = _FakeToolCall(
        [
            {
                "position_id": 10,
                "decision": "eligible",
                "position_type": "phd",
                "opportunity_kind": "vacancy",
                "confidence": 0.96,
                "evidence": ["Applications are invited for a PhD position"],
            },
            {
                "position_id": 11,
                "decision": "rejected",
                "position_type": "other",
                "opportunity_kind": "information",
                "confidence": 0.99,
            },
        ]
    )
    second_call = _FakeToolCall(
        [
            {"position_id": 10, "decision": "wrong", "opportunity_kind": "unknown"},
            {
                "position_id": 11,
                "decision": "rejected",
                "position_type": "other",
                "opportunity_kind": "vacancy",
                "confidence": 0.99,
                "evidence": ["This call is closed"],
            },
        ],
        call_id="call-2",
    )
    completion = AsyncMock(side_effect=[_completion(first_call), _completion(second_call)])
    monkeypatch.setattr("phd_searcher.pipeline.auto_review.acompletion", completion)
    settings = SimpleNamespace(
        llm=SimpleNamespace(model="test-model", api_base=None, api_key="test-key")
    )

    decisions = await _native_ollama_review(
        settings,
        [(_position(10), None), (_position(11), None)],
    )

    assert [item.position_id for item in decisions] == [10, 11]
    assert [item.tool_attempts for item in decisions] == [1, 2]
    assert all(item.latency_seconds is not None for item in decisions)
    assert completion.await_count == 2
    feedback = json.loads(completion.await_args_list[1].kwargs["messages"][-2]["content"])
    assert feedback["accepted_ids"] == [10]
    assert feedback["remaining_ids"] == [11]


@pytest.mark.asyncio
async def test_resilient_review_splits_bad_batch_and_quarantines_only_bad_singleton(monkeypatch):
    rows = [(AsyncMock(id=10), None), (AsyncMock(id=11), None)]

    async def fake_review(_settings, subset):
        position_id = subset[0][0].id
        if len(subset) > 1 or position_id == 11:
            raise RuntimeError("missing decision")
        return [
            AutomaticDecision(
                position_id=position_id,
                decision="eligible",
                position_type="phd",
                opportunity_kind="vacancy",
                confidence=0.99,
                reason="explicit vacancy",
                evidence=["PhD vacancy"],
            )
        ]

    monkeypatch.setattr("phd_searcher.pipeline.auto_review._native_ollama_review", fake_review)
    decisions = await _resilient_ollama_review(AsyncMock(), rows)

    assert decisions[0].decision == "eligible"
    assert decisions[1].decision == "review"
    assert decisions[1].confidence == 0.0
    assert "invalid model tool output" in decisions[1].reason


@pytest.mark.asyncio
async def test_resilient_review_never_reprocesses_valid_partial_results(monkeypatch):
    rows = [(_position(10), None), (_position(11), None)]
    calls = []
    accepted = AutomaticDecision(
        position_id=10,
        decision="eligible",
        position_type="phd",
        opportunity_kind="vacancy",
        confidence=0.99,
        reason="explicit vacancy",
        evidence=["PhD vacancy"],
    )

    async def fake_review(_settings, subset):
        position_ids = [row[0].id for row in subset]
        calls.append(position_ids)
        if position_ids == [10, 11]:
            raise _IncompleteAutomaticReviewError("missing ID 11", [accepted])
        return [
            AutomaticDecision(
                position_id=11,
                decision="rejected",
                position_type="other",
                opportunity_kind="vacancy",
                confidence=0.99,
                reason="closed call",
                evidence=["Applications are closed"],
            )
        ]

    monkeypatch.setattr("phd_searcher.pipeline.auto_review._native_ollama_review", fake_review)
    decisions = await _resilient_ollama_review(AsyncMock(), rows)

    assert calls == [[10, 11], [11]]
    assert [item.position_id for item in decisions] == [10, 11]


def test_review_thresholds_preserve_uncertain_cases():
    eligible = AutomaticDecision(
        position_id=1,
        decision="eligible",
        position_type="phd",
        opportunity_kind="vacancy",
        confidence=0.89,
        reason="likely",
        evidence=["PhD"],
    )
    rejected = AutomaticDecision(
        position_id=2,
        decision="rejected",
        position_type="other",
        opportunity_kind="information",
        confidence=0.94,
        reason="likely",
        evidence=["course"],
    )
    assert _accepted_status(eligible) == "review"
    assert _accepted_status(rejected) == "review"


def test_review_thresholds_accept_high_confidence_decisions():
    eligible = AutomaticDecision(
        position_id=1,
        decision="eligible",
        position_type="assistantship",
        opportunity_kind="vacancy",
        confidence=0.90,
        reason="explicit vacancy",
        evidence=["assistantship vacancy"],
    )
    rejected = AutomaticDecision(
        position_id=2,
        decision="rejected",
        position_type="other",
        opportunity_kind="information",
        confidence=0.98,
        reason="course page",
        evidence=["course catalogue"],
    )
    assert _accepted_status(eligible) == "eligible"
    assert _accepted_status(rejected) == "rejected"


@pytest.mark.parametrize(
    ("opportunity_kind", "evidence", "expected"),
    [
        ("vacancy", "Applications are invited for a funded PhD position", "eligible"),
        ("programme", "Applications are open for the PhD programme", "eligible"),
        ("spontaneous", "We welcome unsolicited applications from PhD candidates", "eligible"),
        ("information", "How to apply to our PhD programme", "review"),
        ("unknown", "Applications are invited for a funded PhD position", "review"),
        ("vacancy", "Applications are open for the PhD programme", "review"),
    ],
)
def test_review_finalizes_only_semantically_coherent_actionable_kinds(
    opportunity_kind,
    evidence,
    expected,
):
    decision = AutomaticDecision(
        position_id=1,
        decision="eligible",
        position_type="phd",
        opportunity_kind=opportunity_kind,
        confidence=0.99,
        reason="model verdict",
        evidence=[evidence],
    )

    assert _accepted_status(decision) == expected


def test_review_state_routes_only_evidence_poor_uncertainty_to_fetch():
    assert _review_state("eligible", "") == "resolved"
    assert _review_state("rejected", "") == "resolved"
    assert _review_state("review", "x" * 199) == "needs_evidence"
    assert _review_state("review", "x" * 200) == "semantic_uncertain"


def test_review_reject_threshold_accepts_calibrated_boundary():
    rejected = AutomaticDecision(
        position_id=3,
        decision="rejected",
        position_type="other",
        opportunity_kind="vacancy",
        confidence=0.97,
        reason="clear non-vacancy",
        evidence=["Currently no vacancies"],
    )
    assert _accepted_status(rejected) == "rejected"


def test_review_never_finalizes_llm_reject_from_listing_snippet_only():
    rejected = AutomaticDecision(
        position_id=3,
        decision="rejected",
        position_type="other",
        opportunity_kind="vacancy",
        confidence=0.99,
        reason="looks closed",
        evidence=["Applications are closed"],
    )
    assert _routed_status(rejected, has_full_description=False) == ("review", True)
    assert _routed_status(rejected, has_full_description=True) == ("rejected", False)


def test_review_bypass_uses_the_attributable_inline_text_boundary():
    position = _position(10)
    position.description = "  " + "x " * 198 + "x  "

    assert len(" ".join(position.description.split())) == 397
    assert not _needs_evidence_bypass(position)

    position.description = " x\n" * 99 + "x"
    assert len(" ".join(position.description.split())) == 199
    assert _needs_evidence_bypass(position)

    position.description = ""
    position.title = "t" * 199
    assert _needs_evidence_bypass(position)

    position.title = "t" * 200
    assert not _needs_evidence_bypass(position)

    position.title = "short"
    position.full_description = "detail exists"
    assert not _needs_evidence_bypass(position)


class _AuditSession:
    def __init__(self):
        self.added = []

    def add(self, value):
        self.added.append(value)


def _routable_position(position_id, *, description, manual=False):
    position = _position(position_id)
    position.description = description
    position.screening_manual = manual
    position.screening_status = "review"
    position.screening_reason = "rules:ambiguous"
    position.screening_source = "rules"
    position.screening_decision = "review"
    position.screening_confidence = None
    position.screening_evidence = None
    position.screening_model = None
    position.screening_version = "hybrid-v3"
    position.screened_at = None
    position.review_state = "untriaged"
    position.routing_reason = None
    position.indexed_at = object()
    return position


@pytest.mark.asyncio
async def test_review_batch_bypasses_poor_text_but_reviews_rich_text(monkeypatch):
    poor = _routable_position(10, description="Too little attributable text")
    rich = _routable_position(11, description="Applications are invited for a PhD position. " * 8)
    manual = _routable_position(12, description="Applications are invited. " * 12, manual=True)
    model_review = AsyncMock(
        return_value=[
            AutomaticDecision(
                position_id=11,
                decision="eligible",
                position_type="phd",
                opportunity_kind="vacancy",
                confidence=0.99,
                reason="explicit vacancy",
                evidence=["Applications are invited for a PhD position"],
            )
        ]
    )
    monkeypatch.setattr("phd_searcher.pipeline.auto_review._resilient_ollama_review", model_review)
    settings = SimpleNamespace(llm=SimpleNamespace(model="ollama/test-model"))
    progress = SimpleNamespace(run_id=42)
    session = _AuditSession()

    await _apply_review_batch(
        settings,
        session,
        [(poor, None), (rich, None), (manual, None)],
        progress,
    )

    model_review.assert_awaited_once()
    assert [row[0].id for row in model_review.await_args.args[1]] == [11]

    assert poor.screening_status == "review"
    assert poor.screening_decision == "review"
    assert poor.review_state == "needs_evidence"
    assert poor.routing_reason == "review1:insufficient_attributable_text"
    assert poor.screening_confidence is None
    assert poor.screening_evidence is None
    assert poor.screening_model is None
    assert poor.screening_source == "router"
    assert poor.screening_version == REVIEW_VERSION == "hybrid-v5"
    assert poor.opportunity_kind == "unknown"
    assert poor.indexed_at is None

    assert rich.screening_status == "eligible"
    assert rich.opportunity_kind == "vacancy"
    assert rich.screening_model == "ollama/test-model"
    assert manual.screening_reason == "rules:ambiguous"
    assert manual.screening_version == "hybrid-v3"

    audits = {attempt.position_id: attempt for attempt in session.added}
    assert set(audits) == {10, 11}
    bypass_audit = audits[10]
    assert bypass_audit.pipeline_run_id == 42
    assert bypass_audit.stage == "review"
    assert bypass_audit.model is None
    assert bypass_audit.tool_attempts == 0
    assert bypass_audit.raw_decision == "review"
    assert bypass_audit.accepted_status == "review"
    assert bypass_audit.evidence == []
    assert bypass_audit.details["review_state"] == "needs_evidence"
    assert bypass_audit.details["llm_bypassed"] is True
    assert bypass_audit.details["opportunity_kind"] == "unknown"
    assert audits[11].details["opportunity_kind"] == "vacancy"


@pytest.mark.asyncio
async def test_review_batch_with_only_poor_text_never_calls_ollama(monkeypatch):
    first = _routable_position(10, description="short")
    second = _routable_position(11, description="")
    second.title = "also short"
    model_review = AsyncMock(side_effect=AssertionError("Ollama must not be called"))
    monkeypatch.setattr("phd_searcher.pipeline.auto_review._resilient_ollama_review", model_review)
    session = _AuditSession()

    await _apply_review_batch(
        SimpleNamespace(llm=SimpleNamespace(model="ollama/test-model")),
        session,
        [(first, None), (second, None)],
        SimpleNamespace(run_id=77),
    )

    model_review.assert_not_awaited()
    assert [attempt.position_id for attempt in session.added] == [10, 11]
    assert all(attempt.pipeline_run_id == 77 for attempt in session.added)
    assert all(attempt.model is None and attempt.tool_attempts == 0 for attempt in session.added)
