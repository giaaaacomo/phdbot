import json
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from phd_searcher.pipeline.deep_review import (
    _ONE_TIME_RETRY_REVIEW_STATES,
    _REQUEUE_ELIGIBLE_VERSIONS,
    _REQUEUE_REJECTED_VERSIONS,
    _RETRYABLE_REVIEW_STATES,
    REVIEW_VERSION,
    DeepReviewResult,
    EvidenceDecision,
    _accepted_status,
    _apply_elapsed_deadline_guard,
    _audit_details,
    _bounded_cohort,
    _cached_verdict,
    _canonical_position_type,
    _deep_review_candidate_filter,
    _deterministic_rule_result,
    _document_can_resolve,
    _elapsed_deadline_rule_result,
    _EvidenceGroundingError,
    _future_deadline_status_conflict_result,
    _IncompleteDeepReviewError,
    _preflight_abstention,
    _prompt,
    _raw_decision,
    _request_batch,
    _resilient_review,
    _result_version,
    _reuse_cached_result,
    _review_fingerprint,
    _review_priority_expression,
    _review_state,
    _tool_failure,
    _validated_decision,
)


def test_review2_requeues_every_legacy_hybrid_version():
    expected = {"hybrid-v2", "hybrid-v3", "hybrid-v4"}
    assert expected <= set(_REQUEUE_ELIGIBLE_VERSIONS)
    assert expected <= set(_REQUEUE_REJECTED_VERSIONS)
    assert set(_RETRYABLE_REVIEW_STATES) == {"ready_deep_review", "tool_error"}
    assert set(_ONE_TIME_RETRY_REVIEW_STATES) == {"grounding_failure"}


def test_deep_review_candidate_filter_excludes_expired_review_rows() -> None:
    sql = str(
        _deep_review_candidate_filter(date(2026, 8, 9)).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    # Currentness is enforced in all three status branches, including review.
    assert sql.count("positions.deadline >= '2026-08-09'") == 3


def test_fresh_attributable_details_have_first_deep_review_priority() -> None:
    sql = str(
        _review_priority_expression().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "positions.detail_cleanup_version = 'attributable-v1'" in sql
    assert "THEN -1" in sql


def _call(arguments):
    return {"function": {"arguments": json.dumps(arguments)}}


def _arguments(
    position_id,
    *,
    actual_vacancy="yes",
    open_status="open",
    position_type="phd",
    opportunity_kind="vacancy",
    evidence_sufficient=True,
    application_evidence=None,
    negative_evidence=None,
    confidence=0.98,
):
    return {
        "position_id": position_id,
        "actual_vacancy": actual_vacancy,
        "open_status": open_status,
        "position_type": position_type,
        "opportunity_kind": opportunity_kind,
        "evidence_sufficient": evidence_sufficient,
        "application_evidence": application_evidence or [],
        "negative_evidence": negative_evidence or [],
        "confidence": confidence,
    }


def _position(position_id, description):
    return SimpleNamespace(
        id=position_id,
        title=f"PhD position {position_id}",
        url=f"https://example.test/{position_id}",
        institution_name="Example University",
        institution_country="IT",
        full_description=description,
        description=description,
        position_type="other",
        opportunity_kind="vacancy",
        deadline=None,
        deadline_raw=None,
    )


class _Response:
    def __init__(self, *, calls=None, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self._calls = calls or []

    def json(self):
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": self._calls,
            }
        }


class _Client:
    def __init__(self, responses, payloads):
        self._responses = responses
        self._payloads = payloads

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url, *, json):
        self._payloads.append(deepcopy(json))
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_deep_review_requires_verbatim_evidence():
    call = _call(
        {
            "position_id": 10,
            "actual_vacancy": "yes",
            "open_status": "open",
            "position_type": "phd",
            "opportunity_kind": "vacancy",
            "evidence_sufficient": True,
            "application_evidence": ["Applications are invited for a funded doctoral position"],
            "negative_evidence": [],
            "confidence": 0.98,
        }
    )
    decision = _validated_decision(
        call,
        contexts={10: "Applications are invited for a funded doctoral position."},
        accepted_ids=set(),
    )
    assert decision is not None

    bad = dict(json.loads(call["function"]["arguments"]))
    bad["application_evidence"] = ["Invented quote"]
    with pytest.raises(ValueError, match="not verbatim"):
        _validated_decision(_call(bad), contexts={10: "Applications are invited."}, accepted_ids=set())


@pytest.mark.parametrize(
    "relative_window",
    [
        "Applications must be submitted within 30 days from publication",
        "Le candidature devono essere presentate entro 30 giorni dalla pubblicazione",
        "Bewerbungsfrist: innerhalb von 30 Tagen nach der Veröffentlichung",
        "Date limite de candidature : dans un délai de 30 jours à compter de la publication",
        "Plazo de presentación de solicitudes: dentro de los 30 días siguientes a la publicación",
    ],
)
def test_deep_review_rejects_open_claim_grounded_only_by_relative_publication_window(
    relative_window,
):
    vacancy = "Funded doctoral position"
    context = f"{vacancy}. {relative_window}."

    with pytest.raises(_EvidenceGroundingError, match="relative application window"):
        _validated_decision(
            _call(
                _arguments(
                    10,
                    application_evidence=[vacancy, relative_window],
                )
            ),
            contexts={10: context},
            accepted_ids=set(),
        )


def test_deep_review_requires_the_asserted_opportunity_kind_to_be_grounded():
    generic = "Information for prospective PhD students"
    procedure = "How to apply using the online application portal"
    context = f"{generic}. {procedure}."

    with pytest.raises(_EvidenceGroundingError, match="opportunity_kind"):
        _validated_decision(
            _call(
                _arguments(
                    10,
                    opportunity_kind="vacancy",
                    application_evidence=[generic, procedure],
                )
            ),
            contexts={10: context},
            accepted_ids=set(),
        )


def test_deep_review_repairs_a_wrong_kind_when_quotes_ground_another_kind():
    quotes = [
        "AI Enabled Supply Chain Resilience in Food Manufacturing",
        "School of Biological Sciences | PHD Funding Funded Reference Number SBIO-2024-004",
        "Application Deadline 31 August 2030",
    ]
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                opportunity_kind="programme",
                application_evidence=quotes,
            )
        ),
        contexts={10: ". ".join(quotes)},
        accepted_ids=set(),
    )

    assert decision is not None
    assert decision.opportunity_kind == "vacancy"


def test_deep_review_repairs_open_window_misfiled_as_negative_evidence():
    title = "ETH Pioneer Fellowships"
    window = (
        "The application portal opens on July 1, 2026 and closes on "
        "Sept 1, 2026 5pm."
    )
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                position_type="research_fellowship",
                application_evidence=[title],
                negative_evidence=[window],
            )
        ),
        contexts={10: f"{title}. {window}"},
        accepted_ids=set(),
    )

    assert decision is not None
    assert decision.application_evidence == [title, window]
    assert decision.negative_evidence == []
    assert _accepted_status(decision) == "eligible"


def test_deep_review_never_moves_a_real_closed_clause_to_positive_evidence():
    title = "Funded doctoral fellowship"
    with pytest.raises(_EvidenceGroundingError, match="does not support"):
        _validated_decision(
            _call(
                _arguments(
                    10,
                    position_type="research_fellowship",
                    application_evidence=[title],
                    negative_evidence=["Applications are closed."],
                )
            ),
            contexts={10: f"{title}. Applications are closed."},
            accepted_ids=set(),
        )


def test_deep_review_adds_exact_title_only_when_it_completes_positive_evidence():
    title = "ETH Zurich - IDEA League Mobility Grants"
    timing = [
        "Requests can be submitted anytime.",
        "four cut off dates per year: 1 March, 1 June, 1 September and 1 December.",
    ]
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                position_type="research_fellowship",
                opportunity_kind="spontaneous",
                application_evidence=timing,
            )
        ),
        contexts={10: " ".join([title, *timing])},
        accepted_ids=set(),
        titles={10: title},
    )

    assert decision is not None
    assert decision.application_evidence[0] == title
    assert decision.opportunity_kind == "vacancy"
    assert _accepted_status(decision) == "eligible"


@pytest.mark.parametrize(
    ("opportunity_kind", "quotes"),
    [
        (
            "programme",
            ["PhD@Work Programme", "Applications for the 2027 intake are now open"],
        ),
        (
            "spontaneous",
            [
                "PhD candidates may send your application directly to the head of the research group",
            ],
        ),
    ],
)
def test_deep_review_accepts_grounded_programme_and_spontaneous_kinds(
    opportunity_kind,
    quotes,
):
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                opportunity_kind=opportunity_kind,
                application_evidence=quotes,
            )
        ),
        contexts={10: ". ".join(quotes)},
        accepted_ids=set(),
    )

    assert decision is not None
    assert decision.opportunity_kind == opportunity_kind
    assert _accepted_status(decision) == "eligible"


def test_deep_review_drops_only_invalid_redundant_citations():
    grounded = "Applications are invited for a funded doctoral position"
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                application_evidence=["PhD", grounded],
            )
        ),
        contexts={10: f"{grounded}."},
        accepted_ids=set(),
    )

    assert decision is not None
    assert decision.application_evidence == [grounded]

    with pytest.raises(ValueError, match="does not support"):
        _validated_decision(
            _call(
                _arguments(
                    10,
                    application_evidence=["Funded opportunity", "PhD"],
                )
            ),
            contexts={10: "Funded opportunity for researchers. PhD."},
            accepted_ids=set(),
        )


def test_deep_review_bounds_excess_tool_evidence_before_schema_validation():
    quotes = [
        "Applications are invited for a funded doctoral position",
        "Submit applications by 31 December 2030",
        "The doctoral position is fully funded",
        "Candidates may apply online before the deadline",
        "A fifth redundant quotation should be safely ignored",
    ]
    context = ". ".join(quotes) + "."

    decision = _validated_decision(
        _call(_arguments(10, application_evidence=quotes)),
        contexts={10: context},
        accepted_ids=set(),
    )

    assert decision is not None
    assert decision.application_evidence == quotes[:4]


def test_deep_review_does_not_repair_wrong_evidence_container_types():
    payload = _arguments(10)
    payload["application_evidence"] = "Applications are invited for a PhD position"

    with pytest.raises(ValueError, match="application_evidence"):
        _validated_decision(
            _call(payload),
            contexts={10: "Applications are invited for a PhD position."},
            accepted_ids=set(),
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("doctoral", "phd"),
        ("research grant", "research_fellowship"),
        ("doctoral scholarship", "phd"),
        ("conference award", "research_fellowship"),
        ("research associate", "research_staff"),
        ("academic", "other"),
        ("Research / teaching assistantship", "assistantship"),
        ("totally invented category", None),
    ],
)
def test_review2_canonicalizes_only_known_position_type_aliases(raw, expected):
    assert _canonical_position_type(raw) == expected


def test_review2_validates_grounding_after_position_type_alias_repair():
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                position_type="doctoral",
                application_evidence=["Applications are invited for a funded doctoral position"],
            )
        ),
        contexts={10: "Applications are invited for a funded doctoral position."},
        accepted_ids=set(),
    )

    assert decision is not None
    assert decision.position_type == "phd"

    with pytest.raises(ValueError, match="unknown position_type"):
        _validated_decision(
            _call(
                _arguments(
                    10,
                    position_type="totally invented category",
                    application_evidence=["Applications are invited for a funded doctoral position"],
                )
            ),
            contexts={10: "Applications are invited for a funded doctoral position."},
            accepted_ids=set(),
        )


def test_review2_repairs_a_wrong_canonical_type_from_exact_local_evidence():
    evidence = [
        "stud. Hilfskraft (m/w/d)(5h/Woche)",
        "Bewerbung senden Sie bitte bis zum 31.08.2030",
    ]
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                position_type="internship",
                application_evidence=evidence,
            )
        ),
        contexts={10: ". ".join(evidence)},
        accepted_ids=set(),
    )

    assert decision is not None
    assert decision.position_type == "assistantship"


def test_review2_repairs_omitted_hilfskraft_title_and_wrong_type_together():
    title = "stud. Hilfskraft (m/w/d)(5h/Woche)"
    deadline = "Bewerbung senden Sie bitte bis zum 31.08.2030"
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                position_type="internship",
                application_evidence=[deadline],
            )
        ),
        contexts={10: f"{title}. {deadline}"},
        accepted_ids=set(),
        titles={10: title},
    )

    assert decision is not None
    assert decision.application_evidence == [title, deadline]
    assert decision.position_type == "assistantship"
    assert decision.opportunity_kind == "vacancy"


def test_review2_repairs_bare_future_date_with_persisted_application_clause():
    title = "stud. Hilfskraft (m/w/d)(5h/Woche)"
    bare_date = "bis zum **31.08.2030**"
    deadline = (
        "Bewerbung: Ihre aussagekräftige Bewerbung senden Sie bitte "
        "bis zum **31.08.2030** per E-Mail."
    )
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                position_type="internship",
                application_evidence=[title],
                negative_evidence=[bare_date],
            )
        ),
        contexts={10: f"{title}. {deadline}"},
        accepted_ids=set(),
        titles={10: title},
        application_anchors={10: deadline},
    )

    assert decision is not None
    assert decision.application_evidence == [title, bare_date, deadline]
    assert decision.negative_evidence == []
    assert decision.position_type == "assistantship"
    assert decision.opportunity_kind == "vacancy"
    assert _accepted_status(decision) == "eligible"


def test_review2_adds_the_exact_intern_title_to_a_short_apply_now_quote():
    title = "Trade Marketing Intern"
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                position_type="internship",
                application_evidence=["Apply Now!"],
            )
        ),
        contexts={10: f"{title}. Apply Now!"},
        accepted_ids=set(),
        titles={10: title},
    )

    assert decision is not None
    assert decision.application_evidence == [title, "Apply Now!"]
    assert decision.opportunity_kind == "vacancy"


def test_deep_review_rejects_trivial_or_semantically_unrelated_quotes():
    context = "Example University publishes information. Applications are invited for a funded doctoral position."
    trivial = _arguments(10, application_evidence=["University"])
    unrelated = _arguments(10, application_evidence=["Example University publishes information"])

    with pytest.raises(ValueError, match="not verbatim"):
        _validated_decision(_call(trivial), contexts={10: context}, accepted_ids=set())
    with pytest.raises(ValueError, match="does not support"):
        _validated_decision(_call(unrelated), contexts={10: context}, accepted_ids=set())


def test_deep_review_requires_negative_quotes_to_support_the_asserted_fact():
    context = "Example University publishes information. Applications are closed. No vacancies are available."
    closed = _arguments(
        10,
        actual_vacancy="unknown",
        open_status="closed",
        application_evidence=[],
        negative_evidence=["Applications are closed"],
    )
    no_vacancy = _arguments(
        10,
        actual_vacancy="no",
        open_status="unknown",
        position_type="other",
        application_evidence=[],
        negative_evidence=["No vacancies are available"],
    )
    unrelated = {**closed, "negative_evidence": ["Example University publishes information"]}

    assert _validated_decision(_call(closed), contexts={10: context}, accepted_ids=set()) is not None
    assert _validated_decision(_call(no_vacancy), contexts={10: context}, accepted_ids=set()) is not None
    with pytest.raises(ValueError, match="does not support"):
        _validated_decision(_call(unrelated), contexts={10: context}, accepted_ids=set())


def test_closed_review_can_ground_deadline_from_the_application_quotes():
    context = (
        "If you would like to apply for a studentship, follow the steps below. "
        "Apply by 4 March 2026. Applications received after this deadline "
        "will not be considered."
    )
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                actual_vacancy="yes",
                open_status="closed",
                position_type="research_fellowship",
                application_evidence=[
                    "If you would like to apply for a studentship, follow the steps below",
                    "Apply by 4 March 2026",
                ],
                negative_evidence=[
                    "Applications received after this deadline will not be considered"
                ],
            )
        ),
        contexts={10: context},
        accepted_ids=set(),
    )

    assert decision is not None
    assert decision.open_status == "closed"


def test_explicit_insufficient_evidence_is_normalized_without_repairing_claims():
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                actual_vacancy="yes",
                open_status="open",
                position_type="invented label",
                evidence_sufficient=False,
                application_evidence=["Invented application quotation"],
                confidence=0.8,
            )
        ),
        contexts={10: "General institutional information."},
        accepted_ids=set(),
    )

    assert decision == EvidenceDecision(
        position_id=10,
        actual_vacancy="unknown",
        open_status="unknown",
        position_type="other",
        opportunity_kind="unknown",
        evidence_sufficient=False,
        application_evidence=[],
        negative_evidence=[],
        confidence=0,
    )


def test_non_vacancy_cannot_carry_an_application_status():
    with pytest.raises(ValueError, match="actual_vacancy=no requires open_status=unknown"):
        _validated_decision(
            _call(
                _arguments(
                    10,
                    actual_vacancy="no",
                    open_status="closed",
                    position_type="other",
                    application_evidence=[],
                    negative_evidence=["No vacancies are available"],
                )
            ),
            contexts={10: "No vacancies are available."},
            accepted_ids=set(),
        )


def test_grounded_other_type_is_repaired_from_application_evidence():
    quote = "Applications are invited for a Contratto di insegnamento"
    decision = _validated_decision(
        _call(
            _arguments(
                10,
                position_type="other",
                application_evidence=[quote],
            )
        ),
        contexts={10: f"{quote}. Submit the application by 31 December."},
        accepted_ids=set(),
    )

    assert decision is not None
    assert decision.position_type == "faculty"


def test_deep_review_composes_conservative_status_from_facts():
    eligible = EvidenceDecision(
        position_id=1,
        actual_vacancy="yes",
        open_status="open",
        position_type="phd",
        opportunity_kind="vacancy",
        evidence_sufficient=True,
        application_evidence=["Apply by 31 December"],
        negative_evidence=[],
        confidence=0.95,
    )
    rejected = EvidenceDecision(
        position_id=2,
        actual_vacancy="yes",
        open_status="closed",
        position_type="phd",
        opportunity_kind="vacancy",
        evidence_sufficient=True,
        application_evidence=[],
        negative_evidence=["Applications are closed"],
        confidence=0.98,
    )
    assert _accepted_status(eligible) == "eligible"
    assert _accepted_status(eligible.model_copy(update={"confidence": 0.90})) == "eligible"
    assert _accepted_status(rejected) == "rejected"
    low_confidence_vague = rejected.model_copy(
        update={
            "confidence": 0.96,
            "negative_evidence": ["This opportunity appears closed"],
        }
    )
    assert _accepted_status(low_confidence_vague) == "review"
    assert _raw_decision(low_confidence_vague) == "rejected"
    assert _accepted_status(eligible.model_copy(update={"evidence_sufficient": False})) == "review"


def test_explicit_closed_status_overrides_only_the_model_confidence_floor():
    explicit_closed = EvidenceDecision(
        position_id=3,
        actual_vacancy="unknown",
        open_status="closed",
        position_type="research_fellowship",
        opportunity_kind="vacancy",
        evidence_sufficient=True,
        application_evidence=[],
        negative_evidence=["Status: CLOSED"],
        confidence=0.50,
    )
    vague_closed = explicit_closed.model_copy(
        update={"negative_evidence": ["This opportunity appears closed"]}
    )

    assert _accepted_status(explicit_closed) == "rejected"
    assert _accepted_status(vague_closed) == "review"


@pytest.mark.asyncio
async def test_review2_is_local_ollama_only():
    settings = SimpleNamespace(llm=SimpleNamespace(model="remote/model", api_base="https://example.test/v1"))

    with pytest.raises(RuntimeError, match="local Ollama"):
        await _request_batch(settings, [(_position(10, "Applications are invited for a PhD position"), None)])


@pytest.mark.asyncio
async def test_review2_uses_tool_feedback_and_retries_only_remaining_ids(monkeypatch):
    first_calls = [
        _call(
            _arguments(
                10,
                application_evidence=["Applications are invited for a PhD position"],
            )
        ),
        _call(_arguments(11, application_evidence=["Invented quotation"])),
    ]
    second_calls = [
        _call(
            _arguments(
                11,
                actual_vacancy="unknown",
                open_status="closed",
                application_evidence=[],
                negative_evidence=["Applications are closed"],
            )
        )
    ]
    responses = [_Response(calls=first_calls), _Response(calls=second_calls)]
    payloads = []
    monkeypatch.setattr(
        "phd_searcher.pipeline.deep_review.httpx.AsyncClient",
        lambda **_kwargs: _Client(responses, payloads),
    )
    settings = SimpleNamespace(llm=SimpleNamespace(model="ollama/gpt-oss:20b", api_base="http://ollama:11434"))
    rows = [
        (_position(10, "Applications are invited for a PhD position"), None),
        (_position(11, "Applications are closed"), None),
    ]

    results = await _request_batch(settings, rows)

    assert [result.decision.position_id for result in results] == [10, 11]
    assert payloads[0]["think"] == "low"
    assert payloads[0]["options"]["num_predict"] == 2_048
    assert payloads[1]["think"] == "low"
    assert payloads[1]["options"]["num_predict"] == 2_048
    assert payloads[1]["messages"][-1]["role"] == "tool"
    assert payloads[1]["messages"][-1]["tool_name"] == "submit_evidence_review"
    feedback = json.loads(payloads[1]["messages"][-1]["content"])
    assert feedback["accepted_ids"] == [10]
    assert feedback["remaining_ids"] == [11]


@pytest.mark.asyncio
async def test_review2_retries_transport_and_http_errors(monkeypatch):
    request = httpx.Request("POST", "http://ollama:11434/api/chat")
    responses = [
        httpx.ConnectError("connection reset", request=request),
        _Response(status_code=503, text="temporarily unavailable"),
        _Response(
            calls=[
                _call(
                    _arguments(
                        10,
                        application_evidence=["Applications are invited for a PhD position"],
                    )
                )
            ]
        ),
    ]
    monkeypatch.setattr(
        "phd_searcher.pipeline.deep_review.httpx.AsyncClient",
        lambda **_kwargs: _Client(responses, []),
    )
    sleep = AsyncMock()
    monkeypatch.setattr("phd_searcher.pipeline.deep_review.asyncio.sleep", sleep)
    settings = SimpleNamespace(llm=SimpleNamespace(model="ollama/gpt-oss:20b", api_base="http://ollama:11434"))

    results = await _request_batch(
        settings,
        [(_position(10, "Applications are invited for a PhD position"), None)],
    )

    assert results[0].decision.position_id == 10
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_final_grounding_failure_becomes_auditable_abstention(monkeypatch):
    unsupported = _call(
        _arguments(
            10,
            application_evidence=["Example University publishes information"],
        )
    )
    responses = [_Response(calls=[unsupported]) for _attempt in range(3)]
    payloads = []
    monkeypatch.setattr(
        "phd_searcher.pipeline.deep_review.httpx.AsyncClient",
        lambda **_kwargs: _Client(responses, payloads),
    )
    settings = SimpleNamespace(
        llm=SimpleNamespace(model="ollama/gpt-oss:20b", api_base="http://ollama:11434")
    )

    results = await _request_batch(
        settings,
        [(_position(10, "Example University publishes information about governance."), None)],
    )

    assert len(payloads) == 3
    assert [payload["think"] for payload in payloads] == ["low", "low", "high"]
    assert [payload["options"]["num_predict"] for payload in payloads] == [
        2_048,
        2_048,
        6_144,
    ]
    assert len(results) == 1
    result = results[0]
    assert result.attempts == 3
    assert result.tool_error is False
    assert result.validation_error is not None
    assert "does not support" in result.validation_error
    assert result.ungrounded_decision is not None
    assert result.ungrounded_decision.actual_vacancy == "yes"
    assert result.ungrounded_decision.application_evidence == [
        "Example University publishes information"
    ]
    assert result.decision == EvidenceDecision(
        position_id=10,
        actual_vacancy="unknown",
        open_status="unknown",
        position_type="other",
        opportunity_kind="unknown",
        evidence_sufficient=False,
        application_evidence=[],
        negative_evidence=[],
        confidence=0,
    )
    assert _review_state(result, "review") == "grounding_failure"
    assert _result_version(result, "hybrid-v3") == REVIEW_VERSION == "evidence-v24"
    assert _audit_details(result) == {
        "actual_vacancy": "unknown",
        "open_status": "unknown",
        "opportunity_kind": "unknown",
        "evidence_sufficient": False,
        "tool_error": False,
        "validation_error": result.validation_error,
        "normalized_type_from": None,
            "rule_reason": None,
            "reused_from_position_id": None,
            "preflight_reason": None,
            "validation_kind": "grounding_failure",
        "ungrounded_decision": result.ungrounded_decision.model_dump(mode="json"),
    }


@pytest.mark.asyncio
async def test_final_unknown_type_is_repaired_from_grounded_document(monkeypatch):
    unknown_type = _call(
        _arguments(
            10,
            position_type="student",
            application_evidence=["Applications are invited for a PhD position"],
        )
    )
    responses = [_Response(calls=[unknown_type]) for _attempt in range(3)]
    monkeypatch.setattr(
        "phd_searcher.pipeline.deep_review.httpx.AsyncClient",
        lambda **_kwargs: _Client(responses, []),
    )
    settings = SimpleNamespace(
        llm=SimpleNamespace(model="ollama/gpt-oss:20b", api_base="http://ollama:11434")
    )

    result = (
        await _request_batch(
            settings,
            [(_position(10, "Applications are invited for a PhD position"), None)],
        )
    )[0]

    assert result.tool_error is False
    assert result.attempts == 3
    assert result.normalized_type_from == "student"
    assert result.decision.position_type == "phd"
    assert _accepted_status(result.decision) == "eligible"


def test_review2_rule_router_rejects_admin_pages_without_calling_a_model():
    administrative = _position(10, "General doctoral administration information")
    administrative.title = "Enrollment"
    administrative.url = "https://example.test/doctoral/enrollment"
    administrative.full_description = (
        "PhD students complete enrollment and pay fees for thesis preparation."
    )
    administrative.description = administrative.full_description
    vacancy = _position(11, "Applications are invited for a PhD position")

    result = _deterministic_rule_result(administrative)

    assert result is not None
    assert result.attempts == 0
    assert result.rule_reason == "administrative_non_vacancy_page"
    assert _accepted_status(result.decision) == "rejected"
    assert _deterministic_rule_result(vacancy) is None


def test_review2_abstains_before_closed_rule_on_euraxess_status_conflict():
    position = _position(
        148,
        "Applications are invited for this funded doctoral position.",
    )
    position.title = "PhD Position eXtended Reality for Inclusive Vehicle Interaction"
    position.url = "https://euraxess.ec.europa.eu/jobs/453993"
    position.position_type = "phd"
    position.deadline = date(2026, 8, 30)
    position.deadline_raw = "30 Aug 2026 - 21:59 (UTC)"
    position.full_description = (
        f"{position.title} ## Job Information "
        f"Application Deadline {position.deadline_raw} "
        "## Offer Description Applications are invited for this funded doctoral position. "
        "## Work Locations Delft ## Contact Example University "
        "STATUS: EXPIRED [Apply now](https://external.example/apply/) "
        "##### Share this page"
    )
    position.description = "Applications are invited for this funded doctoral position."

    # The conflict guard runs before deterministic rules, caches and the model
    # in the canonical router, preserving the contradictory source evidence.
    conflict = _future_deadline_status_conflict_result(
        position,
        today=date(2026, 8, 26),
    )

    assert conflict is not None
    assert conflict.attempts == 0
    assert conflict.preflight_reason == "future_deadline_status_conflict"
    assert _accepted_status(conflict.decision) == "review"


@pytest.mark.asyncio
async def test_malformed_tool_payload_remains_retryable_tool_error(monkeypatch):
    malformed = {"function": {"arguments": "{not valid json"}}
    responses = [_Response(calls=[malformed]) for _attempt in range(3)]
    monkeypatch.setattr(
        "phd_searcher.pipeline.deep_review.httpx.AsyncClient",
        lambda **_kwargs: _Client(responses, []),
    )
    settings = SimpleNamespace(
        llm=SimpleNamespace(model="ollama/gpt-oss:20b", api_base="http://ollama:11434")
    )

    results = await _resilient_review(
        settings,
        [(_position(10, "Applications are invited for a PhD position"), None)],
    )

    assert len(results) == 1
    result = results[0]
    assert result.tool_error is True
    assert result.validation_error is not None
    assert "Expecting property name" in result.validation_error
    assert _review_state(result, "review") == "tool_error"


@pytest.mark.asyncio
async def test_partial_batch_gets_a_fresh_singleton_retry(monkeypatch):
    rows = [
        (_position(10, "Applications are invited for a PhD position"), None),
        (_position(11, "Applications are closed"), None),
    ]
    accepted = DeepReviewResult(
        decision=EvidenceDecision.model_validate(
            _arguments(10, application_evidence=["Applications are invited for a PhD position"])
        ),
        attempts=1,
        latency_seconds=1,
    )
    calls = []

    async def fake_request(_settings, subset, **_kwargs):
        ids = [row[0].id for row in subset]
        calls.append(ids)
        if ids == [10, 11]:
            raise _IncompleteDeepReviewError("missing 11", [accepted])
        return [
            DeepReviewResult(
                decision=EvidenceDecision.model_validate(
                    _arguments(
                        11,
                        actual_vacancy="unknown",
                        open_status="closed",
                        application_evidence=[],
                        negative_evidence=["Applications are closed"],
                    )
                ),
                attempts=1,
                latency_seconds=1,
            )
        ]

    monkeypatch.setattr("phd_searcher.pipeline.deep_review._request_batch", fake_request)

    results = await _resilient_review(AsyncMock(), rows)

    assert calls == [[10, 11], [11]]
    assert [result.decision.position_id for result in results] == [10, 11]


def test_review2_cohort_is_bounded_only_for_incomplete_review1_with_a_run():
    assert not _bounded_cohort({}, 42)
    assert not _bounded_cohort({"cohort_complete": True}, 42)
    assert not _bounded_cohort({"cohort_complete": False}, None)
    assert _bounded_cohort({"cohort_complete": False}, 42)


def test_review2_tool_failure_does_not_stamp_the_final_version():
    failure = _tool_failure(10, latency_seconds=1)
    success = DeepReviewResult(
        decision=EvidenceDecision.model_validate(
            _arguments(10, application_evidence=["Applications are invited for a PhD position"])
        ),
        attempts=1,
        latency_seconds=1,
    )

    assert _result_version(failure, "hybrid-v3") == "hybrid-v3"
    assert _result_version(failure, None) is None
    assert _result_version(success, "hybrid-v3") == REVIEW_VERSION == "evidence-v24"
    assert _review_state(failure, "review") == "tool_error"
    assert _review_state(success, "eligible") == "resolved"

    insufficient = success.__class__(
        decision=success.decision.model_copy(update={"evidence_sufficient": False}),
        attempts=1,
        latency_seconds=1,
    )
    assert _review_state(insufficient, "review") == "source_unusable"
    assert insufficient.validation_error is None
    assert insufficient.ungrounded_decision is None


def test_review2_preflight_skips_documents_that_cannot_pass_its_validator():
    generic = _position(10, "General information about the department and its research themes.")
    open_call = _position(
        11,
        "Applications are invited for a funded PhD position. Apply by 31 December.",
    )
    closed_call = _position(12, "This doctoral vacancy is closed.")

    assert not _document_can_resolve(generic)
    result = _preflight_abstention(generic)
    assert result is not None
    assert result.attempts == 0
    assert result.preflight_reason == "no_decisive_signals"
    assert _review_state(result, "review") == "source_unusable"
    assert _document_can_resolve(open_call)
    assert _preflight_abstention(open_call) is None
    assert _document_can_resolve(closed_call)


def test_review2_preflight_keeps_grants_and_research_associate_calls():
    grant = _position(
        13,
        "Convocatoria de ayudas para congresos. Plazo de presentación de solicitudes: 31 diciembre 2030.",
    )
    grant.title = "Ayudas para participación en congresos"
    associate = _position(
        14,
        "Applications are invited for a Research Associate position. Apply by 31 December 2030.",
    )
    associate.title = "Research Associate in Fluid Dynamics"

    assert _document_can_resolve(grant)
    assert _preflight_abstention(grant) is None
    assert _document_can_resolve(associate)
    assert _preflight_abstention(associate) is None


def test_review2_prompt_defines_broad_opportunities_and_current_date():
    position = _position(10, "Applications are invited for a PhD position")
    prompt = _prompt(
        [(position, None)],
        {10: position.description},
        today=date(2026, 8, 9),
    )

    assert "travel/conference grants" in prompt
    assert "already students" in prompt
    assert "Today's date is 2026-08-09" in prompt
    assert "application-window end before today means closed" in prompt
    assert "Classify opportunity_kind independently" in prompt
    assert "'How to apply' proves only a procedure" in prompt


def test_elapsed_deadline_guard_overrides_only_grounded_positive_calls():
    document = (
        "Convocatoria de ayudas para congresos. Formalización de solicitudes y "
        "plazo de presentación. Los estudiantes podrán solicitar esta ayuda "
        "entre el 1 y el 15 de julio de 2026."
    )
    position = _position(18107, document)
    result = DeepReviewResult(
        decision=EvidenceDecision.model_validate(
            _arguments(
                18107,
                open_status="future",
                position_type="research_fellowship",
                application_evidence=[
                    "Convocatoria de ayudas para congresos",
                    "entre el 1 y el 15 de julio de 2026",
                ],
                confidence=0.95,
            )
        ),
        attempts=1,
        latency_seconds=1,
    )

    guarded = _apply_elapsed_deadline_guard(result, position, today=date(2026, 8, 9))

    assert guarded.rule_reason == "application_deadline_elapsed"
    assert guarded.decision.actual_vacancy == "yes"
    assert guarded.decision.open_status == "closed"
    assert guarded.decision.confidence == 1
    assert position.deadline == date(2026, 7, 15)
    assert _accepted_status(guarded.decision) == "rejected"

    unchanged = _apply_elapsed_deadline_guard(result, position, today=date(2026, 7, 10))
    assert unchanged is result


def test_elapsed_deadline_guard_repairs_a_persisted_interview_date():
    document = (
        "The application deadline is Sept 1, 2026 (5 pm, Swiss time). "
        "After the application deadline candidates will be informed. "
        "The interview will take place on October 29, 2026."
    )
    position = _position(17398, document)
    position.deadline = date(2026, 10, 29)
    position.deadline_raw = "interview October 29, 2026"
    result = DeepReviewResult(
        decision=EvidenceDecision.model_validate(
            _arguments(
                17398,
                position_type="research_fellowship",
                application_evidence=[
                    "ETH Pioneer Fellowships",
                    "The application deadline is Sept 1, 2026 (5 pm, Swiss time)",
                ],
            )
        ),
        attempts=1,
        latency_seconds=1,
    )

    guarded = _apply_elapsed_deadline_guard(result, position, today=date(2026, 8, 18))

    assert guarded is result
    assert position.deadline == date(2026, 9, 1)
    assert position.deadline_raw is not None
    assert "application deadline" in position.deadline_raw.casefold()


def test_elapsed_deadline_rule_resolves_recognizable_calls_before_the_model():
    document = (
        "Convocatoria de ayudas para congresos. Formalización de solicitudes y "
        "plazo de presentación. Los estudiantes podrán solicitar esta ayuda "
        "entre el 1 y el 15 de julio de 2026."
    )
    position = _position(18107, document)
    position.title = "Convocatoria de ayudas para participación en Congresos"

    result = _elapsed_deadline_rule_result(position, today=date(2026, 8, 9))

    assert result is not None
    assert result.attempts == 0
    assert result.rule_reason == "application_deadline_elapsed"
    assert result.decision.position_type == "research_fellowship"
    assert result.decision.open_status == "closed"
    assert _accepted_status(result.decision) == "rejected"

    generic = _position(12, "The programme application deadline was 15 July 2026.")
    generic.title = "Programme information"
    assert _elapsed_deadline_rule_result(generic, today=date(2026, 8, 9)) is None

    localized = _position(
        13,
        "The Solvay Brussels School currently has a phd position available. "
        "The vacancy is open till the 15th of June 2022, but early applications "
        "are encouraged.",
    )
    localized.title = "Doctoral student in Supply Chain Management"
    localized_result = _elapsed_deadline_rule_result(
        localized,
        today=date(2026, 8, 18),
    )
    assert localized_result is not None
    assert localized_result.rule_reason == "application_deadline_elapsed"
    assert _accepted_status(localized_result.decision) == "rejected"


def test_exact_review_fingerprint_includes_institution_and_temporal_state():
    first = _position(10, "Applications are invited for a funded doctoral position")
    second = _position(11, first.description)
    for position in (first, second):
        position.title = "PhD position in AI"
        position.deadline = date(2030, 12, 31)
        position.deadline_raw = "31 December 2030"
        position.published_at = date(2030, 1, 1)
        position.published_raw = "1 January 2030"
        position.screening_status = "eligible"
        position.screening_evidence = json.dumps(
            ["Applications are invited for a funded doctoral position"]
        )
        position.screening_confidence = 0.95
        position.position_type = "phd"
    university = SimpleNamespace(id=7, name="Example University", country="IT")

    assert _review_fingerprint(first, university) == _review_fingerprint(second, university)
    second.deadline = date(2031, 1, 1)
    assert _review_fingerprint(first, university) != _review_fingerprint(second, university)


def test_cached_verdict_reuses_only_a_grounded_accepted_decision():
    position = _position(10, "Applications are invited for a funded doctoral position")
    position.screening_status = "eligible"
    position.screening_evidence = json.dumps(
        ["Applications are invited for a funded doctoral position"]
    )
    position.screening_confidence = 0.95
    position.position_type = "phd"

    cached = _cached_verdict(position)
    assert cached is not None
    reused = _reuse_cached_result(99, cached)
    assert reused.decision.position_id == 99
    assert reused.reused_from_position_id == 10
    assert reused.attempts == 0


def test_cached_verdict_rejects_stale_or_semantically_unsupported_evidence():
    position = _position(10, "The department publishes general information.")
    position.screening_status = "eligible"
    position.screening_evidence = json.dumps(["Applications are invited for a PhD position"])
    position.screening_confidence = 0.99
    position.position_type = "phd"
    position.screening_source = "llm"

    assert _cached_verdict(position) is None

    position.screening_evidence = json.dumps(["The department publishes general information"])
    assert _cached_verdict(position) is None
