from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from phd_searcher.config import Settings
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.pipeline.auto_review import AutomaticDecision
from scripts.benchmark_auto_review import (
    Candidate,
    GoldLabel,
    GoldSet,
    Reference,
    TelemetryRecorder,
    build_report,
    candidate_content_sha256,
    compare_models,
    evaluate_model,
    load_candidates,
    load_gold_set,
    normalize_ollama_model,
    render_markdown,
    score_gold,
    stratified_sample,
    summarize_model,
    validate_gold_candidates,
    warm_up_model,
)


def _candidate(
    position_id: int,
    *,
    position_type: str = "phd",
    review_state: str = "semantic_uncertain",
    full_description: str | None = "Applications are invited for an open doctoral position.",
) -> Candidate:
    position = Position(
        id=position_id,
        title=f"Candidate {position_id}",
        url=f"https://example.test/jobs/{position_id}",
        description="Applications are invited for an open doctoral position.",
        full_description=full_description,
        position_type=position_type,
        opportunity_kind="unknown",
        institution_name="Example University",
        institution_country="IT",
        screening_manual=False,
    )
    return Candidate(
        position=position,
        university=None,
        reference=Reference(
            screening_status="review",
            screening_decision="review",
            screening_source="llm",
            screening_manual=False,
            review_state=review_state,
            position_type=position_type,
            opportunity_kind="unknown",
        ),
    )


def _decision(position_id: int, routed_status: str, *, valid: bool = True) -> dict[str, object]:
    return {
        "position_id": position_id,
        "title": f"Candidate {position_id}",
        "decision": routed_status,
        "routed_status": routed_status,
        "position_type": "phd",
        "opportunity_kind": "vacancy",
        "confidence": 0.99,
        "latency_seconds": float(position_id),
        "contract_valid": valid,
        "reference_status": "review",
        "reference_is_manual": False,
    }


def _gold_label(
    position_id: int,
    expected_status: str,
    *,
    accepted_types: tuple[str, ...] = (),
    accepted_kinds: tuple[str, ...] = (),
    content_sha256: str = "0" * 64,
) -> GoldLabel:
    return GoldLabel(
        position_id=position_id,
        expected_status=expected_status,
        accepted_position_types=accepted_types,
        accepted_opportunity_kinds=accepted_kinds,
        content_sha256=content_sha256,
        rationale="Human judgement from attributable evidence.",
    )


def test_normalize_ollama_model_preserves_qualified_models_and_rejects_empty_names():
    assert normalize_ollama_model("gpt-oss:20b") == "ollama/gpt-oss:20b"
    assert normalize_ollama_model("ollama/qwen3.6:35b-a3b") == "ollama/qwen3.6:35b-a3b"
    with pytest.raises(ValueError, match="cannot be empty"):
        normalize_ollama_model("  ")


def test_stratified_sample_is_fixed_and_uses_distinct_strata_before_repeats():
    candidates = [
        _candidate(1),
        _candidate(2),
        _candidate(3, position_type="postdoc"),
        _candidate(4, review_state="ready_deep_review"),
    ]

    first = stratified_sample(candidates, sample_size=3, seed=42)
    second = stratified_sample(candidates, sample_size=3, seed=42)

    assert [candidate.position_id for candidate in first] == [candidate.position_id for candidate in second]
    assert len({candidate.stratum for candidate in first}) == 3


def test_telemetry_uses_ollama_usage_and_detects_invalid_tool_arguments():
    recorder = TelemetryRecorder()
    feedback = json.dumps(
        {
            "error": "invalid_or_incomplete_reviews",
            "details": "missing position IDs: [2]",
            "remaining_ids": [2],
        }
    )
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://ollama/api/chat"),
        json={
            "prompt_eval_count": 100,
            "eval_count": 25,
            "total_duration": 2_000_000_000,
            "message": {
                "tool_calls": [
                    {
                        "function": {
                            "name": "submit_position_reviews",
                            "arguments": "{not-json",
                        }
                    }
                ]
            },
        },
    )

    recorder.record_response(
        {"messages": [{"role": "tool", "content": feedback}]},
        response,
        elapsed=2.1,
    )

    request = recorder.requests[0]
    assert request.prompt_tokens == 100
    assert request.completion_tokens == 25
    assert request.total_duration_seconds == 2.0
    assert request.position_ids == (2,)
    assert recorder.request_exposures == {2: 1}
    assert request.malformed_json is True
    assert request.validation_feedback == "missing position IDs: [2]"


def test_retry_accounting_attributes_follow_up_only_to_remaining_ids():
    recorder = TelemetryRecorder()
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://ollama/api/chat"),
        json={"message": {}},
    )
    feedback = json.dumps(
        {
            "error": "invalid_or_incomplete_reviews",
            "remaining_ids": [2],
            "details": "missing position IDs: [2]",
        }
    )

    with recorder.candidate_scope([1, 2]):
        recorder.record_response({"messages": []}, response, elapsed=1.0)
        recorder.record_response(
            {"messages": [{"role": "tool", "content": feedback}]},
            response,
            elapsed=1.0,
        )

    assert recorder.request_exposures == {1: 1, 2: 2}
    summary = summarize_model(
        [_decision(1, "review"), _decision(2, "review")],
        recorder,
        wall_seconds=2.0,
    )
    assert summary["decisions_requiring_retry"] == 1
    assert summary["mean_request_exposures"] == 1.5


def test_summaries_separate_contract_failure_from_conservative_review():
    recorder = TelemetryRecorder()
    decisions = [_decision(1, "eligible"), _decision(2, "review", valid=False)]

    summary = summarize_model(decisions, recorder, wall_seconds=3.0)

    assert summary["contract_valid"] == 1
    assert summary["fallback_decisions"] == 1
    assert summary["routed_status_counts"] == {"eligible": 1, "review": 1}
    assert summary["decision_latency_p50_seconds"] == 1.0
    assert summary["decision_latency_p95_seconds"] == 2.0


def test_summary_uses_requested_candidates_when_a_run_returns_only_a_prefix():
    summary = summarize_model(
        [_decision(1, "eligible")],
        TelemetryRecorder(),
        wall_seconds=3.0,
        requested_position_ids=[1, 2, 3],
    )

    assert summary["candidates"] == 3
    assert summary["requested_candidates"] == 3
    assert summary["returned_decisions"] == 1
    assert summary["missing_decisions"] == 2
    assert summary["missing_position_ids"] == [2, 3]
    assert summary["completion_rate"] == pytest.approx(1 / 3)
    assert summary["contract_valid_rate"] == pytest.approx(1 / 3)
    assert summary["returned_contract_valid_rate"] == 1.0
    assert summary["technical_failures"] == 2


def test_gold_scoring_keeps_accuracy_coverage_and_abstention_distinct():
    labels = {
        1: _gold_label(1, "eligible", accepted_types=("phd",), accepted_kinds=("vacancy",)),
        2: _gold_label(2, "rejected"),
        3: _gold_label(3, "eligible", accepted_types=("phd",), accepted_kinds=("vacancy",)),
        4: _gold_label(4, "rejected"),
    }
    decisions = [
        _decision(1, "eligible"),
        _decision(2, "eligible"),
        _decision(3, "review"),
    ]

    metrics = score_gold(decisions, labels)

    assert metrics["correct"] == 1
    assert metrics["incorrect"] == 1
    assert metrics["abstentions"] == 2
    assert metrics["technical_abstentions"] == 1
    assert metrics["coverage"] == pytest.approx(0.5)
    assert metrics["accuracy"] == pytest.approx(0.25)
    assert metrics["selective_accuracy"] == pytest.approx(0.5)
    assert metrics["abstention_rate"] == pytest.approx(0.5)
    assert metrics["false_acceptances"] == 1
    assert metrics["joint_accuracy"] == pytest.approx(0.25)


def test_gold_loader_rejects_review_as_a_truth_label(tmp_path: Path):
    path = tmp_path / "gold.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "name": "test",
                "version": "v1",
                "labelled_at": "2026-08-27",
                "labels": [
                    {
                        "position_id": 1,
                        "expected_status": "review",
                        "accepted_position_types": [],
                        "accepted_opportunity_kinds": [],
                        "content_sha256": "0" * 64,
                        "rationale": "uncertain",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="abstention, not ground truth"):
        load_gold_set(path)


def test_gold_validation_detects_changed_candidate_content(tmp_path: Path):
    candidate = _candidate(1)
    label = _gold_label(
        1,
        "eligible",
        accepted_types=("phd",),
        accepted_kinds=("vacancy",),
        content_sha256=candidate_content_sha256(candidate),
    )
    gold = GoldSet(
        name="test",
        version="v1",
        labelled_at="2026-08-27",
        valid_until=date(2099, 1, 1),
        labels=(label,),
        source_path=tmp_path / "gold.json",
    )
    validate_gold_candidates([candidate], gold)
    candidate.position.description = "Changed after adjudication."

    with pytest.raises(ValueError, match="content changed"):
        validate_gold_candidates([candidate], gold)


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        ("title", "A changed title"),
        ("url", "https://example.test/jobs/changed"),
        ("description", "Changed attributable listing text."),
        ("full_description", "Changed full evidence."),
        ("position_type", "postdoc"),
        ("institution_name", "Changed University"),
        ("institution_country", "CH"),
        ("deadline", date(2099, 2, 3)),
        ("deadline_raw", "3 February 2099"),
    ],
)
def test_gold_digest_covers_every_position_field_used_by_prompt_or_evidence(
    field_name: str,
    changed_value: object,
):
    candidate = _candidate(1)
    original = candidate_content_sha256(candidate)

    setattr(candidate.position, field_name, changed_value)

    assert candidate_content_sha256(candidate) != original


def test_gold_digest_covers_linked_university_name_and_country():
    candidate = _candidate(1)
    university = University(
        wikidata_id="benchmark:1",
        name="Example University",
        country="IT",
        website_url="",
    )
    linked = Candidate(
        position=candidate.position,
        university=university,
        reference=candidate.reference,
    )
    original = candidate_content_sha256(linked)

    university.name = "Changed University"
    assert candidate_content_sha256(linked) != original
    university.name = "Example University"
    university.country = "CH"
    assert candidate_content_sha256(linked) != original


@pytest.mark.asyncio
async def test_warmup_is_explicitly_excluded_from_metrics(monkeypatch: pytest.MonkeyPatch):
    async def fake_review(*_args: object, **_kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(
        "scripts.benchmark_auto_review.auto_review._resilient_ollama_review",
        fake_review,
    )
    result = await warm_up_model(Settings(), _candidate(1))

    assert result["excluded_from_metrics"] is True
    assert result["position_id"] == 1
    assert result["requests"] == 0


@pytest.mark.asyncio
async def test_evaluate_model_applies_override_without_mutating_baseline_settings(
    monkeypatch: pytest.MonkeyPatch,
):
    seen_models: list[str] = []

    async def fake_warmup(settings, _candidate):
        seen_models.append(settings.llm.model)
        return {"excluded_from_metrics": True}

    async def fake_review(settings, rows):
        seen_models.append(settings.llm.model)
        return [
            AutomaticDecision(
                position_id=position.id,
                decision="review",
                position_type="other",
                opportunity_kind="unknown",
                confidence=0.5,
                reason="Evidence remains ambiguous.",
                evidence=["Applications are invited for an open doctoral position."],
            )
            for position, _university in rows
        ]

    monkeypatch.setattr("scripts.benchmark_auto_review.warm_up_model", fake_warmup)
    monkeypatch.setattr(
        "scripts.benchmark_auto_review.auto_review._resilient_ollama_review",
        fake_review,
    )
    settings = Settings()
    baseline_model = settings.llm.model
    result = await evaluate_model(
        settings,
        "qwen3.6:35b-a3b",
        [_candidate(1)],
        batch_size=1,
    )

    assert seen_models == ["ollama/qwen3.6:35b-a3b"] * 2
    assert result["model"] == "ollama/qwen3.6:35b-a3b"
    assert settings.llm.model == baseline_model


@pytest.mark.asyncio
async def test_candidate_loader_sets_read_only_before_select(monkeypatch: pytest.MonkeyPatch):
    statements: list[str] = []

    class _Result:
        def mappings(self):
            return self

        def all(self):
            return []

    class _AsyncContext:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *_args):
            return None

    class _Connection:
        def begin(self):
            return _AsyncContext(None)

        async def execute(self, statement, _params=None):
            statements.append(str(statement).strip())
            return _Result()

    class _Engine:
        def __init__(self):
            self.connection = _Connection()

        def connect(self):
            return _AsyncContext(self.connection)

        async def dispose(self):
            return None

    monkeypatch.setattr(
        "scripts.benchmark_auto_review.create_async_engine",
        lambda _url: _Engine(),
    )
    settings = SimpleNamespace(database=SimpleNamespace(url="postgresql+asyncpg://unused"))

    candidates = await load_candidates(
        settings,
        ids=[],
        cohort="review",
        sample_size=1,
        seed=7,
    )

    assert candidates == []
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert statements[1].startswith("SELECT p.id")


def test_comparison_reports_only_routed_disagreements():
    left = {
        "model": "ollama/left",
        "decisions": [_decision(1, "eligible"), _decision(2, "review")],
    }
    right = {
        "model": "ollama/right",
        "decisions": [_decision(1, "eligible"), _decision(2, "rejected")],
    }

    comparison = compare_models([left, right])[0]

    assert comparison["routed_status_agreement"] == pytest.approx(0.5)
    assert [row["position_id"] for row in comparison["disagreements"]] == [2]


def test_report_records_fixed_ids_and_renders_a_compact_markdown_handoff():
    candidates = [_candidate(10), _candidate(20, position_type="postdoc")]
    decisions = [_decision(10, "eligible"), _decision(20, "review")]
    model = {
        "model": "ollama/test",
        "summary": summarize_model(decisions, TelemetryRecorder(), wall_seconds=1.25),
        "requests": [],
        "terminal_failures": [],
        "decisions": decisions,
    }

    report = build_report(
        candidates=candidates,
        results=[model],
        cohort="review",
        seed=7,
        batch_size=8,
    )
    markdown = render_markdown(report)

    assert report["read_only"] is True
    assert report["sample"]["position_ids"] == [10, 20]
    assert "ollama/test" in markdown
    assert "Stored decisions are a drift reference" in markdown


def test_gold_markdown_surfaces_joint_accuracy():
    report = {
        "gold_set": {"version": "v1"},
        "sample": {"position_ids": [1]},
        "models": [
            {
                "model": "ollama/test",
                "summary": {
                    "gold": {
                        "accuracy": 1.0,
                        "joint_accuracy": 0.5,
                        "coverage": 1.0,
                        "selective_accuracy": 1.0,
                        "abstention_rate": 0.0,
                    },
                    "contract_valid": 1,
                    "candidates": 1,
                    "requests": 1,
                    "wall_seconds": 1.0,
                },
            }
        ],
        "comparisons": [],
    }

    markdown = render_markdown(report)

    assert "Joint accuracy" in markdown
    assert "| ollama/test | 100.0% | 50.0% |" in markdown
