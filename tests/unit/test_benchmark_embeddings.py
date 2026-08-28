import json
import math
from pathlib import Path

import pytest

from scripts.benchmark_embeddings import (
    DEFAULT_GOLD_SET,
    Candidate,
    ModelSpec,
    aggregate_metrics,
    aggregate_threshold_curves,
    build_candidate_document,
    build_judged_score_report,
    build_threshold_curve,
    clean_text,
    cosine_normalized,
    evaluate_ranking,
    format_document,
    format_query,
    gold_position_ids,
    load_gold_set,
    normalize_vector,
    parse_model_spec,
    prepare_query_input,
    ranked_candidates,
    reciprocal_rank_fusion,
    update_threshold_counts,
    update_top_k,
)


def _candidate(position_id: int, title: str) -> Candidate:
    return Candidate(
        position_id=position_id,
        title=title,
        position_type="phd",
        institution="Example University",
        description="Short listing excerpt",
        area="Human-computer interaction",
        full_description="Full page text that should not leak into the default document",
    )


def test_clean_text_removes_markup_decodes_entities_and_truncates_at_word_boundary():
    assert clean_text(" <p>Design&nbsp; for   health</p> ") == "Design for health"
    assert clean_text("one two three", max_chars=8) == "one two…"


def test_build_candidate_document_defaults_to_short_candidate_specific_description():
    document = build_candidate_document(_candidate(1, "XR researcher"))

    assert "Title: XR researcher" in document
    assert "Position type: phd" in document
    assert "Institution: Example University" in document
    assert "Description: Short listing excerpt" in document
    assert "Full page text" not in document


def test_build_candidate_document_can_explicitly_use_full_description():
    document = build_candidate_document(
        _candidate(1, "XR researcher"),
        include_full_description=True,
    )

    assert "Full page text that should not leak" in document
    assert "Short listing excerpt" not in document


def test_build_candidate_document_area_is_an_explicit_benchmark_only_variant():
    candidate = _candidate(1, "XR researcher")

    compact = build_candidate_document(candidate)
    compact_area = build_candidate_document(candidate, include_area=True)

    assert "Human-computer interaction" not in compact
    assert "Research area: Human-computer interaction" in compact_area


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("nomic-embed-text", ModelSpec("nomic-embed-text", "nomic")),
        ("ollama/nomic-embed-text::raw", ModelSpec("nomic-embed-text", "raw")),
        ("qwen3-embedding:0.6b", ModelSpec("qwen3-embedding:0.6b", "qwen")),
    ],
)
def test_parse_model_spec_handles_ollama_tags_and_profiles(raw, expected):
    assert parse_model_spec(raw) == expected


def test_embedding_profiles_apply_provider_contracts():
    assert format_document("candidate", "nomic") == "search_document: candidate"
    assert format_query("virtual reality", "nomic") == "search_query: virtual reality"
    assert format_document("candidate", "qwen") == "candidate"
    qwen_query = format_query("virtual reality", "qwen")
    assert qwen_query.startswith("Instruct: ")
    assert qwen_query.endswith("Query: virtual reality")
    assert format_query("virtual reality", "raw") == "virtual reality"


def test_benchmark_uses_production_query_normalization_by_default():
    retrieval_query, model_input = prepare_query_input("XR and VR", "nomic")

    assert retrieval_query == "extended reality and virtual reality"
    assert model_input == "search_query: extended reality and virtual reality"
    assert prepare_query_input(
        "XR and VR",
        "nomic",
        normalize_retrieval=False,
    ) == ("XR and VR", "search_query: XR and VR")


def test_vector_normalization_and_cosine_validate_dimensions():
    assert normalize_vector([3.0, 4.0]) == pytest.approx((0.6, 0.8))
    assert cosine_normalized((1.0, 0.0), (0.5, 0.5)) == pytest.approx(0.5)
    with pytest.raises(ValueError, match="dimension mismatch"):
        cosine_normalized((1.0,), (1.0, 0.0))
    with pytest.raises(ValueError, match="zero norm"):
        normalize_vector([0.0, 0.0])


def test_streaming_top_k_is_bounded_ranked_and_deterministic():
    heap = []
    for position_id, score in ((1, 0.4), (2, 0.9), (3, 0.7), (4, 0.7)):
        update_top_k(
            heap,
            score=score,
            candidate=_candidate(position_id, f"Candidate {position_id}"),
            top_k=3,
        )

    assert len(heap) == 3
    results = ranked_candidates(heap)
    assert [(result.position_id, result.score) for result in results] == [
        (2, 0.9),
        (4, 0.7),
        (3, 0.7),
    ]


def test_versioned_gold_set_loads_known_queries_and_stable_ids():
    judgments = load_gold_set(DEFAULT_GOLD_SET)

    assert judgments["biodesign"] == {1210: 2}
    assert judgments["spatial computing"] == {
        10836: 2,
        10835: 1,
        148: 1,
        4998: 1,
        6591: 1,
        24842: 1,
    }
    assert gold_position_ids(judgments) == sorted(gold_position_ids(judgments))
    assert {1210, 32045, 89659} <= set(gold_position_ids(judgments))


def test_gold_set_rejects_duplicate_grades(tmp_path: Path):
    path = tmp_path / "invalid-gold.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "judgments": {"query": {"2": [123], "1": [123]}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate grades"):
        load_gold_set(path)


def test_evaluate_ranking_calculates_graded_and_grade_2_metrics():
    metrics = evaluate_ranking([2, 1, 99], {1: 2, 2: 1, 3: 2}, cutoff=3)
    expected_dcg = 1.0 + 3.0 / math.log2(3)
    ideal_dcg = 3.0 + 3.0 / math.log2(3) + 1.0 / math.log2(4)

    assert metrics["ndcg_at_3"] == pytest.approx(expected_dcg / ideal_dcg)
    assert metrics["grade_2_recall_at_3"] == pytest.approx(0.5)
    assert metrics["grade_2_mrr_at_3"] == pytest.approx(0.5)
    assert metrics["judged_positions"] == 3
    assert metrics["grade_2_positions"] == 2


def test_evaluate_ranking_keeps_grade_2_metrics_undefined_without_grade_2():
    metrics = evaluate_ranking([1], {1: 1}, cutoff=20)

    assert metrics["ndcg_at_20"] == pytest.approx(1.0)
    assert metrics["grade_2_recall_at_20"] is None
    assert metrics["grade_2_mrr_at_20"] is None


def test_threshold_curve_counts_entire_corpus_and_known_grade_2_recall():
    thresholds = (0.45, 0.60, 0.70)
    counts = [0, 0, 0]
    scores = {1: 0.70, 2: 0.60, 3: 0.59, 4: 0.20}
    for score in scores.values():
        update_threshold_counts(counts, score=score, thresholds=thresholds)

    curve = build_threshold_curve(
        thresholds=thresholds,
        returned_counts=counts,
        judgments={1: 2, 2: 2, 3: 1, 5: 2},
        judged_scores=scores,
    )

    assert counts == [3, 2, 1]
    assert curve == [
        {
            "threshold": 0.45,
            "returned": 3,
            "grade_2_retrieved": 2,
            "grade_2_recall": pytest.approx(2 / 3),
        },
        {
            "threshold": 0.60,
            "returned": 2,
            "grade_2_retrieved": 2,
            "grade_2_recall": pytest.approx(2 / 3),
        },
        {
            "threshold": 0.70,
            "returned": 1,
            "grade_2_retrieved": 1,
            "grade_2_recall": pytest.approx(1 / 3),
        },
    ]


def test_threshold_curve_without_judgments_still_reports_exact_volume():
    assert build_threshold_curve(thresholds=[0.5], returned_counts=[12]) == [
        {
            "threshold": 0.5,
            "returned": 12,
            "grade_2_retrieved": None,
            "grade_2_recall": None,
        }
    ]


def test_judged_score_report_keeps_missing_ids_visible_and_orders_by_grade():
    assert build_judged_score_report(
        {3: 1, 2: 2, 1: 2},
        {1: 0.61234567, 3: 0.4},
    ) == [
        {"position_id": 1, "grade": 2, "score": 0.612346},
        {"position_id": 2, "grade": 2, "score": None},
        {"position_id": 3, "grade": 1, "score": 0.4},
    ]


def test_aggregate_threshold_curves_reports_volume_and_macro_recall():
    reports = [
        {
            "threshold_curve": [
                {"threshold": 0.5, "returned": 10, "grade_2_recall": 1.0},
                {"threshold": 0.6, "returned": 4, "grade_2_recall": 0.5},
            ]
        },
        {
            "threshold_curve": [
                {"threshold": 0.5, "returned": 6, "grade_2_recall": None},
                {"threshold": 0.6, "returned": 2, "grade_2_recall": None},
            ]
        },
    ]

    assert aggregate_threshold_curves(reports) == [
        {
            "threshold": 0.5,
            "total_returned": 16,
            "mean_returned": 8.0,
            "macro_grade_2_recall": 1.0,
            "judged_queries": 1,
        },
        {
            "threshold": 0.6,
            "total_returned": 6,
            "mean_returned": 3.0,
            "macro_grade_2_recall": 0.5,
            "judged_queries": 1,
        },
    ]


def test_aggregate_metrics_macro_averages_available_queries_only():
    reports = [
        {
            "metrics": {
                "ndcg_at_20": 1.0,
                "grade_2_recall_at_20": 0.5,
                "grade_2_mrr_at_20": 1.0,
            }
        },
        {
            "metrics": {
                "ndcg_at_20": 0.5,
                "grade_2_recall_at_20": None,
                "grade_2_mrr_at_20": None,
            }
        },
    ]

    aggregate = aggregate_metrics(reports)

    assert aggregate == {
        "judged_queries": 2,
        "ndcg_at_20": pytest.approx(0.75),
        "grade_2_recall_at_20": pytest.approx(0.5),
        "grade_2_mrr_at_20": pytest.approx(1.0),
    }


def test_reciprocal_rank_fusion_rewards_cross_model_agreement() -> None:
    runs = [
        {
            "label": "precise",
            "queries": [
                {
                    "query": "xr",
                    "retrieval_query": "extended reality",
                    "results": [
                        {"rank": 1, "score": 0.8, "position_id": 1, "title": "A"},
                        {"rank": 2, "score": 0.7, "position_id": 2, "title": "B"},
                    ],
                }
            ],
        },
        {
            "label": "multilingual",
            "queries": [
                {
                    "query": "xr",
                    "retrieval_query": "extended reality",
                    "results": [
                        {"rank": 1, "score": 0.6, "position_id": 2, "title": "B"},
                        {"rank": 2, "score": 0.5, "position_id": 3, "title": "C"},
                    ],
                }
            ],
        },
    ]

    fusion = reciprocal_rank_fusion(
        runs,
        top_k=3,
        gold_judgments={"xr": {2: 2}},
    )

    assert fusion is not None
    first = fusion["queries"][0]["results"][0]
    assert first["position_id"] == 2
    assert first["source_ranks"] == {"precise": 2, "multilingual": 1}
    assert fusion["metrics"]["grade_2_mrr_at_20"] == pytest.approx(1.0)
