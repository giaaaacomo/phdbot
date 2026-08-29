"""Regression tests for routing opportunity kinds to the two semantic indexes."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import ClauseElement

from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.opportunity_kinds import (
    INFORMATION,
    PROGRAMME,
    SPONTANEOUS,
    UNKNOWN,
    VACANCY,
    OpportunityKind,
)
from phd_searcher.pipeline.family_feedback import FamilyFeedbackSignal
from phd_searcher.pipeline.index import (
    _embed_and_upsert,
    _family_payload,
    _invalidate_missing_collection,
    _positions_to_index_stmt,
    _remove_orphan_vectors,
    _sync_observation_payload,
    _sync_opportunity_kind_payload,
    _unsearchable_indexed_stmt,
    _verification_metadata,
    is_provisional_eligible,
)
from phd_searcher.pipeline.institutions import _build_entities
from phd_searcher.pipeline.source_family import FamilyDirection


def _university(
    university_id: int,
    name: str,
    *,
    spontaneous_application_url: str | None = None,
) -> University:
    return University(
        id=university_id,
        wikidata_id=f"Q{university_id}",
        name=name,
        country="IT",
        website_url=f"https://uni-{university_id}.example",
        description=f"{name} description",
        spontaneous_application_url=spontaneous_application_url,
    )


def test_negative_family_prior_marks_probable_uncertainty_without_rejecting() -> None:
    verification, payload = _family_payload(
        ("probable", None, 15, ()),
        FamilyFeedbackSignal(
            direction=FamilyDirection.SUPPORTS_NON_OPPORTUNITY,
            samples=20,
            family_key="listing:7|template:example.test/jobs/{id}",
        ),
    )

    assert verification == ("probable", None, 60, ("source_family",))
    assert payload == ("supports_non_opportunity", 20)


async def test_observation_timestamps_sync_without_reembedding(
    qdrant: AsyncQdrantClient,
) -> None:
    await qdrant.create_collection(
        "positions",
        vectors_config=VectorParams(size=4, distance=Distance.COSINE),
    )
    await qdrant.upsert(
        "positions",
        points=[PointStruct(id=7, vector=[1.0, 0.0, 0.0, 0.0], payload={})],
    )
    first_seen = datetime(2026, 8, 20, 8)
    last_seen = datetime(2026, 8, 24, 9)
    result = SimpleNamespace(all=lambda: [(7, first_seen, last_seen)])
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    synced = await _sync_observation_payload(qdrant, "positions", session)

    assert synced == 1
    points = await qdrant.retrieve("positions", ids=[7], with_payload=True)
    assert points[0].payload == {
        "first_seen_at": "2026-08-20T08:00:00+00:00",
        "last_seen_at": "2026-08-24T09:00:00+00:00",
        "scraped_at": "2026-08-24T09:00:00+00:00",
    }


def _position(
    position_id: int,
    opportunity_kind: OpportunityKind,
    *,
    university_id: int | None = 1,
    listing_page_id: int | None = None,
    institution_name: str | None = None,
    research_group: str | None = None,
    screening_status: str = "eligible",
    screening_decision: str | None = None,
    screening_confidence: float | None = None,
    screening_manual: bool | None = None,
    screening_source: str | None = None,
    screening_evidence: str | None = None,
    screening_version: str | None = None,
    is_active: bool = True,
    deadline: date | None = None,
    title: str | None = None,
    url: str | None = None,
    description: str | None = None,
    full_description: str | None = None,
    position_type: str = "phd",
    review_state: str = "semantic_uncertain",
    routing_reason: str | None = None,
) -> Position:
    if screening_manual is None:
        screening_manual = screening_status == "eligible"
    if screening_source is None:
        screening_source = "manual" if screening_manual else "rules"
    return Position(
        id=position_id,
        university_id=university_id,
        listing_page_id=listing_page_id,
        institution_name=institution_name,
        institution_country="IT" if university_id is None else None,
        url=url or f"https://opportunity.example/{position_id}",
        title=title or f"Opportunity {position_id}",
        description=description or f"Description {position_id}",
        full_description=full_description,
        opportunity_kind=opportunity_kind,
        position_type=position_type,
        screening_status=screening_status,
        screening_decision=screening_decision,
        screening_confidence=screening_confidence,
        screening_manual=screening_manual,
        screening_source=screening_source,
        screening_evidence=screening_evidence,
        screening_version=screening_version,
        review_state=review_state,
        routing_reason=routing_reason,
        research_group=research_group,
        is_active=is_active,
        deadline=deadline,
    )


def _compiled(statement: ClauseElement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_position_index_query_preserves_verified_and_adds_coarse_provisional_candidates() -> None:
    sql = _compiled(_positions_to_index_stmt(date(2026, 8, 9)))
    assert "positions.screening_status = 'eligible'" in sql
    assert "positions.opportunity_kind IN ('vacancy', 'programme')" in sql
    assert "positions.screening_status IN ('pending', 'review', 'eligible')" in sql
    assert "positions.opportunity_kind IN ('unknown', 'vacancy', 'programme')" in sql
    assert "positions.is_active IS true" in sql

    cleanup_sql = _compiled(_unsearchable_indexed_stmt(date(2026, 8, 9)))
    assert "positions.deadline < '2026-08-09'" in cleanup_sql
    assert "positions.screening_status IN ('pending', 'review', 'eligible')" in cleanup_sql



def test_provisional_gate_accepts_current_rule_clean_candidate() -> None:
    position = _position(
        1,
        UNKNOWN,
        screening_status="review",
        title="PhD position in robotics",
        description="Applications are now open for this PhD position in robotics.",
    )

    assert is_provisional_eligible(position, today=date(2026, 8, 9))
    assert _verification_metadata(position, date(2026, 8, 9)) == (
        "probable",
        None,
        15,
        (),
    )


def test_provisional_gate_labels_missing_open_timing_instead_of_hiding_candidate() -> None:
    position = _position(
        15,
        UNKNOWN,
        screening_status="review",
        title="PhD position in robotics",
        description="Application procedure for this PhD position in robotics.",
    )

    assert _verification_metadata(position, date(2026, 8, 9)) == (
        "probable",
        None,
        35,
        ("open_status",),
    )


def test_provisional_gate_exposes_strong_title_as_labelled_high_recall_lead() -> None:
    position = _position(
        16,
        UNKNOWN,
        screening_status="review",
        title="PhD position in marine engineering",
        description="Research project on sustainable ship design.",
    )

    assert _verification_metadata(position, date(2026, 8, 9)) == (
        "probable",
        None,
        60,
        ("open_status", "details"),
    )


def test_provisional_gate_keeps_conflicting_euraxess_status_as_uncertain_lead() -> None:
    title = "PhD Position eXtended Reality for Inclusive Vehicle Interaction"
    full_description = (
        f"navigation {title} ## Job Information "
        "Application Deadline 30 Aug 2026 - 21:59 (UTC) "
        "## Offer Description Applications are invited for this doctoral position. "
        "## Work Locations Delft ## Contact City Delft "
        "STATUS: EXPIRED [Apply now](https://academictransfer.example/apply/) "
        "##### Share this page"
    )
    position = _position(
        18,
        VACANCY,
        screening_status="eligible",
        screening_manual=False,
        title=title,
        url="https://euraxess.ec.europa.eu/jobs/453992",
        description="Inclusive Virtual Reality research position.",
        full_description=full_description,
        deadline=date(2026, 8, 30),
    )
    position.deadline_raw = "30 Aug 2026 - 21:59 (UTC)"

    assert _verification_metadata(position, date(2026, 8, 26)) == (
        "probable",
        None,
        60,
        ("open_status", "details"),
    )


def test_legacy_rule_positive_is_probable_instead_of_silently_verified() -> None:
    position = _position(
        17,
        VACANCY,
        screening_status="eligible",
        screening_manual=False,
        screening_source="rules",
        title="PhD position in marine engineering",
        description="Applications are now open for this PhD position in marine engineering.",
    )

    assert _verification_metadata(position, date(2026, 8, 9)) == (
        "probable",
        None,
        15,
        (),
    )


def test_ungrounded_legacy_positive_remains_available_at_explicit_high_uncertainty() -> None:
    position = _position(
        18,
        PROGRAMME,
        screening_status="eligible",
        screening_manual=False,
        screening_source="rules",
        title="Research opportunity in advanced materials",
        description="Explore advanced materials research at our university.",
        position_type="other",
    )

    assert _verification_metadata(position, date(2026, 8, 9)) == (
        "probable",
        None,
        85,
        ("verification", "open_status", "details"),
    )


def test_manual_positive_is_verified() -> None:
    position = _position(19, VACANCY, screening_confidence=1.0)

    assert _verification_metadata(position, date(2026, 8, 9)) == (
        "verified",
        1.0,
        0,
        (),
    )


def test_grounded_evidence_positive_is_verified() -> None:
    quote = (
        "Applications are now open for this PhD position in robotics; "
        "the application deadline is 31 December 2026."
    )
    position = _position(
        20,
        VACANCY,
        screening_status="eligible",
        screening_decision="eligible",
        screening_confidence=0.95,
        screening_manual=False,
        screening_source="llm",
        screening_evidence=json.dumps([quote]),
        screening_version="evidence-v23",
        review_state="resolved",
        title="PhD position in robotics",
        description=quote,
    )

    assert _verification_metadata(position, date(2026, 8, 9)) == (
        "verified",
        0.95,
        0,
        (),
    )


@pytest.mark.parametrize(
    ("quality_status", "expected"),
    [
        ("healthy", True),
        ("unknown", False),
        ("degraded", False),
        ("quarantine", False),
    ],
)
def test_provisional_gate_requires_healthy_listing_quality(
    quality_status: str,
    expected: bool,
) -> None:
    position = _position(
        30,
        VACANCY,
        listing_page_id=5,
        screening_status="review",
        title="PhD position in robotics",
        description="Applications are now open for this PhD position in robotics.",
    )
    listing = ListingPage(
        id=5,
        url="https://university.example/jobs",
        quality_status=quality_status,
    )

    assert (
        is_provisional_eligible(
            position,
            listing_page=listing,
            today=date(2026, 8, 9),
        )
        is expected
    )


def test_provisional_gate_allows_euraxess_but_not_weak_orphan() -> None:
    euraxess = _position(
        31,
        VACANCY,
        listing_page_id=6,
        screening_status="review",
        title="Doctoral researcher",
        url="https://euraxess.ec.europa.eu/jobs/123456",
        description="Applications are now open for this doctoral researcher position.",
    )
    unknown_listing = ListingPage(
        id=6,
        url="https://euraxess.ec.europa.eu/jobs/search",
        quality_status="unknown",
    )
    weak_orphan = _position(
        32,
        UNKNOWN,
        screening_status="review",
        title="Research opportunities",
        position_type="other",
    )

    assert is_provisional_eligible(
        euraxess,
        listing_page=unknown_listing,
        today=date(2026, 8, 9),
    )
    assert not is_provisional_eligible(
        weak_orphan,
        today=date(2026, 8, 9),
    )


def test_audited_curated_portal_exposes_generic_job_as_labelled_lead() -> None:
    position = _position(
        33,
        UNKNOWN,
        listing_page_id=7,
        screening_status="review",
        title="Interaction Designer",
        url="https://jobs.example.edu/job/view/interaction-designer",
        description="Example University · published 3 August 2026",
        position_type="other",
    )
    listing = ListingPage(
        id=7,
        university_id=1,
        url="https://jobs.example.edu/site/index",
        source="seed",
        schema_status="ok",
        quality_status="healthy",
    )

    assert _verification_metadata(
        position,
        date(2026, 8, 9),
        listing_page=listing,
    ) == (
        "probable",
        None,
        60,
        ("open_status", "details"),
    )


def test_global_seed_source_does_not_get_curated_institution_exception() -> None:
    position = _position(
        34,
        UNKNOWN,
        university_id=None,
        listing_page_id=8,
        screening_status="review",
        title="Interaction Designer",
        position_type="other",
    )
    listing = ListingPage(
        id=8,
        university_id=None,
        url="https://example.test/jobs",
        source="seed",
        schema_status="ok",
        quality_status="healthy",
    )

    assert not is_provisional_eligible(
        position,
        listing_page=listing,
        today=date(2026, 8, 9),
    )


@pytest.mark.parametrize(
    ("position_id", "title", "deadline", "deadline_raw"),
    [
        (
            148,
            "PhD Position eXtended Reality for Inclusive Automated Vehicle Interaction",
            date(2026, 8, 30),
            "30 Aug 2026 - 21:59 (UTC)",
        ),
        (
            1210,
            "Biodesign for Interaction: An Exploratory Approach (Biodesign for Interaction, B4I) - 2026_IDR_DESIGN_8",
            date(2026, 8, 31),
            "31 Aug 2026 - 12:00 (UTC)",
        ),
        (
            4998,
            "Postdoc in Application of eXtended Reality for Inclusive Automated Vehicle and Road User Interaction",
            date(2026, 8, 30),
            "30 Aug 2026 - 21:59 (UTC)",
        ),
        (
            6591,
            "DESIGN AND ARCHITECTURE OF PHYSIOLOGY-AWARE ADAPTIVE SYSTEMS FOR WELL-BEING IN EXTENDED REALITY - 2026_IDR_DEIB_52",
            date(2026, 9, 1),
            "1 Sep 2026 - 12:00 (UTC)",
        ),
    ],
)
def test_euraxess_status_conflict_keeps_current_xr_candidates_as_uncertain(
    position_id: int,
    title: str,
    deadline: date,
    deadline_raw: str,
) -> None:
    full_description = (
        f"navigation {title} ## Job Information Organisation Example University "
        f"Application Deadline {deadline_raw} Country Italy "
        "## Offer Description Applications are invited for this research position in "
        "interaction design, Virtual Reality and eXtended Reality. "
        "## Work Locations Example City ## Contact Example University "
        "STATUS: EXPIRED [Apply now](https://external.example/apply/) "
        "##### Share this page"
    )
    position = _position(
        position_id,
        VACANCY,
        screening_status="eligible",
        screening_manual=False,
        screening_source="rules",
        title=title,
        url=f"https://euraxess.ec.europa.eu/jobs/{position_id}",
        description="Virtual Reality and interaction design research position.",
        full_description=full_description,
        deadline=deadline,
        position_type=(
            "postdoc"
            if position_id == 4998
            else "research_fellowship"
            if position_id in {1210, 6591}
            else "phd"
        ),
        review_state="resolved",
    )
    position.deadline_raw = deadline_raw

    assert _verification_metadata(position, date(2026, 8, 26)) == (
        "probable",
        None,
        60,
        ("open_status", "details"),
    )


@pytest.mark.parametrize(
    "position",
    [
        _position(2, VACANCY, screening_status="rejected"),
        _position(3, VACANCY, screening_status="quarantine"),
        _position(4, VACANCY, screening_status="review", is_active=False),
        _position(
            5,
            VACANCY,
            screening_status="review",
            deadline=date(2026, 8, 8),
        ),
        _position(6, INFORMATION, screening_status="review"),
        _position(7, SPONTANEOUS, screening_status="review"),
        _position(
            8,
            VACANCY,
            screening_status="review",
            review_state="source_broken",
        ),
        _position(
            9,
            VACANCY,
            screening_status="review",
            review_state="fetch_unavailable",
        ),
        _position(
            10,
            VACANCY,
            screening_status="review",
            review_state="fetch_failed",
        ),
        _position(
            14,
            VACANCY,
            screening_status="review",
            review_state="source_unusable",
            title="PhD position in robotics",
            description="Applications are now open for this PhD position in robotics.",
        ),
        _position(
            11,
            VACANCY,
            screening_status="review",
            routing_reason="evidence:unsupported_asset",
        ),
        _position(
            12,
            VACANCY,
            screening_status="review",
            url="https://opportunity.example/poster.png",
        ),
        _position(
            13,
            VACANCY,
            screening_status="review",
            title="Page not found",
        ),
    ],
)
def test_provisional_gate_hard_exclusions(position: Position) -> None:
    assert not is_provisional_eligible(position, today=date(2026, 8, 9))


@pytest.mark.parametrize(
    "position",
    [
        _position(
            40,
            VACANCY,
            listing_page_id=8,
            screening_status="review",
            title="Chef",
            url="https://university.example/jobs/chef",
            description="Applications are now open for this role.",
            position_type="other",
        ),
        _position(
            41,
            VACANCY,
            listing_page_id=8,
            screening_status="review",
            title="ERC grants",
            url="https://university.example/jobs/erc-grants",
            description="Information about ERC grants and research funding.",
            position_type="research_fellowship",
        ),
    ],
)
def test_provisional_gate_does_not_promote_portal_noise(position: Position) -> None:
    listing = ListingPage(
        id=8,
        url="https://university.example/jobs",
        quality_status="healthy",
    )

    assert not is_provisional_eligible(
        position,
        listing_page=listing,
        today=date(2026, 8, 9),
    )


@pytest.mark.parametrize("title", ["Administrative Vacancies", "Operational Vacancies"])
def test_provisional_gate_does_not_rescue_category_heading_from_shared_listing_text(
    title: str,
) -> None:
    position = _position(
        42,
        VACANCY,
        listing_page_id=8,
        screening_status="review",
        title=title,
        description=(
            "Applications are now open for a neighbouring doctoral researcher "
            "position. Apply by 30 September 2026."
        ),
    )
    listing = ListingPage(
        id=8,
        url="https://university.example/jobs",
        quality_status="healthy",
    )

    assert not is_provisional_eligible(
        position,
        listing_page=listing,
        today=date(2026, 8, 9),
    )


async def test_full_position_cleanup_removes_orphan_vectors(qdrant: AsyncQdrantClient) -> None:
    await qdrant.create_collection(
        "positions",
        vectors_config=VectorParams(size=4, distance=Distance.COSINE),
    )
    await qdrant.upsert(
        "positions",
        points=[
            PointStruct(id=1, vector=[1.0, 0.0, 0.0, 0.0]),
            PointStruct(id=999, vector=[1.0, 0.0, 0.0, 0.0]),
        ],
    )

    assert await _remove_orphan_vectors(qdrant, "positions", {1}) == 1
    points, _ = await qdrant.scroll("positions", limit=10, with_payload=False, with_vectors=False)
    assert {point.id for point in points} == {1}


async def test_missing_collection_invalidates_stale_database_flags() -> None:
    indexed_at = datetime(2026, 8, 9, tzinfo=UTC).replace(tzinfo=None)
    positions = [SimpleNamespace(indexed_at=indexed_at), SimpleNamespace(indexed_at=indexed_at)]
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: positions))
    session = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
    qdrant = SimpleNamespace(collection_exists=AsyncMock(return_value=False))

    invalidated = await _invalidate_missing_collection(qdrant, "positions", session)

    assert invalidated == 2
    assert all(position.indexed_at is None for position in positions)
    session.commit.assert_awaited_once()


async def test_existing_collection_keeps_database_index_flags() -> None:
    session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    qdrant = SimpleNamespace(collection_exists=AsyncMock(return_value=True))

    assert await _invalidate_missing_collection(qdrant, "positions", session) == 0
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_full_refresh_backfills_derived_provisional_metadata_without_reembedding() -> None:
    positions = [
        _position(
            1,
            VACANCY,
            screening_status="eligible",
            screening_confidence=0.98,
        ),
        _position(
            2,
            UNKNOWN,
            screening_status="review",
            screening_decision="eligible",
            screening_confidence=0.72,
            title="Marketing Intern",
            description=(
                "Applications are now open for this internship position. "
                "The university also operates an international graduate school programme."
            ),
            position_type="other",
        ),
        _position(
            3,
            PROGRAMME,
            screening_status="eligible",
            title="Marketing Intern",
            description="Applications are now open for this internship position.",
            position_type="other",
        ),
        _position(
            4,
            PROGRAMME,
            screening_status="eligible",
            screening_manual=False,
            screening_source="rules",
            title="Research opportunity in advanced materials",
            description="Explore advanced materials research at our university.",
            position_type="other",
        ),
    ]
    result = SimpleNamespace(all=lambda: [(position, None) for position in positions])
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    qdrant = SimpleNamespace(
        collection_exists=AsyncMock(return_value=True),
        set_payload=AsyncMock(),
    )

    synced = await _sync_opportunity_kind_payload(
        qdrant,
        "positions",
        session,
        family_profiles={},
        today=date(2026, 8, 9),
    )

    assert synced == 4
    assert qdrant.set_payload.await_count == 4
    payloads = {
        (
            call.kwargs["payload"]["position_type"],
            call.kwargs["payload"]["opportunity_kind"],
            call.kwargs["payload"]["verification_status"],
            call.kwargs["payload"]["confidence"],
            call.kwargs["payload"]["uncertainty_percent"],
            tuple(call.kwargs["payload"]["uncertainty_flags"]),
        ): set(call.kwargs["points"])
        for call in qdrant.set_payload.await_args_list
    }
    assert payloads == {
        ("phd", VACANCY, "verified", 0.98, 0, ()): {1},
        ("internship", VACANCY, "probable", 0.72, 15, ()): {2},
        ("other", PROGRAMME, "verified", None, 0, ()): {3},
        (
            "other",
            UNKNOWN,
            "probable",
            None,
            85,
            ("verification", "open_status", "details"),
        ): {4},
    }


async def test_upsert_derives_only_provisional_metadata_and_preserves_confidence(
    qdrant: AsyncQdrantClient,
) -> None:
    provisional = _position(
        20,
        UNKNOWN,
        screening_status="review",
        screening_decision="eligible",
        screening_confidence=0.74,
        title="Marketing Intern",
        description=(
            "Applications are now open for this internship position. "
            "The university also operates an international graduate school programme."
        ),
        position_type="other",
    )
    verified = _position(
        21,
        PROGRAMME,
        screening_status="eligible",
        title="Marketing Intern",
        description="Applications are now open for this internship position.",
        position_type="other",
    )
    provisional.scraped_at = datetime(2026, 8, 9)
    verified.scraped_at = datetime(2026, 8, 9)
    embed_documents = AsyncMock(
            return_value=[
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
            ]
        )
    model = SimpleNamespace(
        embed_documents=embed_documents,
        search_index_contract=lambda: "candidate-compact-v2|raw-v1|test/model",
    )

    await _embed_and_upsert(
        model,
        qdrant,
        "positions",
        [
            (provisional, _university(1, "Test University"), None),
            (verified, _university(1, "Test University"), None),
        ],
        family_profiles={},
        today=date(2026, 8, 9),
    )

    points, _ = await qdrant.scroll(
        "positions",
        limit=10,
        with_payload=True,
        with_vectors=False,
    )
    payload_by_id = {point.id: point.payload for point in points}
    assert payload_by_id[20]["verification_status"] == "probable"
    assert payload_by_id[20]["confidence"] == 0.74
    assert payload_by_id[20]["uncertainty_percent"] == 15
    assert payload_by_id[20]["uncertainty_flags"] == []
    assert payload_by_id[20]["position_type"] == "internship"
    assert payload_by_id[20]["opportunity_kind"] == VACANCY
    assert payload_by_id[21]["verification_status"] == "verified"
    assert payload_by_id[21]["uncertainty_percent"] == 0
    assert payload_by_id[21]["uncertainty_flags"] == []
    assert payload_by_id[21]["position_type"] == "other"
    assert payload_by_id[21]["opportunity_kind"] == PROGRAMME
    assert payload_by_id[20]["_phdbot_search_index_contract"] == (
        "candidate-compact-v2|raw-v1|test/model"
    )
    embedded_documents = embed_documents.await_args.args[0]
    assert "Position type: internship" in embedded_documents[0]
    assert "Position type: other" in embedded_documents[1]
    assert provisional.position_type == "other"
    assert provisional.opportunity_kind == UNKNOWN


def test_spontaneous_opportunities_feed_institutions_without_counting_as_positions() -> None:
    curated_url = "https://curated.example/apply"
    curated = _university(1, "Curated University", spontaneous_application_url=curated_url)
    fallback = _university(2, "Fallback University")
    rows = [
        (_position(10, VACANCY, research_group="Mixed Lab"), curated),
        (_position(11, SPONTANEOUS, research_group="Mixed Lab"), curated),
        (_position(12, SPONTANEOUS, research_group="Open Lab"), curated),
        (_position(13, UNKNOWN, research_group="Legacy Ghost Lab"), curated),
        (_position(14, SPONTANEOUS, university_id=2), fallback),
        (
            _position(
                15,
                SPONTANEOUS,
                university_id=None,
                institution_name="Independent Institute",
            ),
            None,
        ),
        (_position(16, INFORMATION, research_group="Information Ghost Lab"), curated),
        (_position(17, PROGRAMME, screening_status="review"), curated),
        (_position(18, VACANCY, deadline=date(2026, 8, 8)), curated),
    ]

    entities = _build_entities([curated, fallback], rows, today=date(2026, 8, 9))
    by_name = {str(entity["name"]): entity for entity in entities}

    assert set(by_name) == {
        "Curated University",
        "Fallback University",
        "Mixed Lab",
        "Open Lab",
        "Independent Institute",
    }
    assert by_name["Curated University"]["active_positions"] == 1
    assert by_name["Curated University"]["spontaneous_application_url"] == curated_url
    assert by_name["Fallback University"]["active_positions"] == 0
    assert by_name["Fallback University"]["spontaneous_application_url"] == (
        "https://opportunity.example/14"
    )
    assert by_name["Mixed Lab"]["active_positions"] == 1
    assert by_name["Mixed Lab"]["spontaneous_application_url"] == (
        "https://opportunity.example/11"
    )
    assert by_name["Open Lab"]["active_positions"] == 0
    assert by_name["Open Lab"]["spontaneous_application_url"] == (
        "https://opportunity.example/12"
    )
    assert by_name["Independent Institute"]["kind"] == "institution"
    assert by_name["Independent Institute"]["active_positions"] == 0
    assert by_name["Independent Institute"]["spontaneous_application_url"] == (
        "https://opportunity.example/15"
    )
