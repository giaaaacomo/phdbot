from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError

from phd_searcher.database.models.position import Position
from phd_searcher.database.models.position_feedback import PositionFeedback
from phd_searcher.service.feedback_service import FeedbackService
from phd_searcher.typedef.feedback import PositionFeedbackCreate


class _FeedbackSession:
    def __init__(self, position: Position | None) -> None:
        self.position = position
        self.feedback: PositionFeedback | None = None
        self.commits = 0

    async def __aenter__(self) -> _FeedbackSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, model: type[object], object_id: int) -> object | None:
        if model is Position:
            return self.position if self.position and self.position.id == object_id else None
        if model is PositionFeedback:
            return self.feedback if self.feedback and self.feedback.id == object_id else None
        return None

    def add(self, feedback: PositionFeedback) -> None:
        feedback.id = 1
        feedback.created_at = datetime(2026, 8, 21, tzinfo=UTC).replace(tzinfo=None)
        self.feedback = feedback

    async def execute(self, _statement: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _feedback: PositionFeedback) -> None:
        return None


class _FeedbackSessionMaker:
    def __init__(self, session: _FeedbackSession) -> None:
        self.session = session

    def __call__(self) -> _FeedbackSession:
        return self.session


def _position() -> Position:
    return Position(
        id=7,
        url="https://example.test/jobs/7",
        title="PhD position",
        description="Open PhD position",
        position_type="phd",
        opportunity_kind="vacancy",
        screening_status="review",
        is_active=True,
        indexed_at=datetime(2026, 8, 21),
    )


def test_feedback_model_is_structured_and_position_preserving() -> None:
    table = PositionFeedback.__table__
    assert table.name == "position_feedback"
    assert table.columns["position_id"].foreign_keys
    foreign_key = next(iter(table.columns["position_id"].foreign_keys))
    assert foreign_key.ondelete == "RESTRICT"
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_position_feedback_reason",
        "ck_position_feedback_status",
    }
    assert table.columns["retracted_at"].nullable
    assert table.columns["source_family_keys"].nullable
    assert "uq_position_feedback_open_dimension" in {index.name for index in table.indexes}


def test_feedback_body_validates_reason_and_normalizes_note() -> None:
    body = PositionFeedbackCreate(reason="non_opportunity", note="  wrong page  ")
    assert body.note == "wrong page"
    assert PositionFeedbackCreate(reason="other", note="   ").note is None
    assert PositionFeedbackCreate(reason="confirmed_opportunity").reason == "confirmed_opportunity"
    with pytest.raises(ValidationError):
        PositionFeedbackCreate.model_validate({"reason": "ban_domain"})


async def test_feedback_create_and_retract_are_auditable_without_mutating_position() -> None:
    position = _position()
    indexed_at = position.indexed_at
    session = _FeedbackSession(position)
    service = FeedbackService(
        cast(Any, _FeedbackSessionMaker(session))
    )

    created = await service.create(
        position.id,
        PositionFeedbackCreate(reason="wrong_type", note="This is a master programme"),
    )

    assert created is not None
    assert created.status == "open"
    assert created.dimension == "type"
    assert created.value == "wrong"
    assert created.source_family_version == "url-family-v1"
    assert created.source_family_keys
    assert session.feedback is not None
    assert session.feedback.reason == "wrong_type"
    assert position.indexed_at == indexed_at
    assert position.screening_status == "review"

    retracted = await service.retract(position.id, created.id)

    assert retracted is not None
    assert retracted.status == "retracted"
    assert retracted.retracted_at is not None
    assert session.feedback is not None
    assert session.feedback.status == "retracted"
    assert position.indexed_at == indexed_at
    assert session.commits == 2


async def test_positive_feedback_is_opportunity_evidence_not_an_issue_alias() -> None:
    position = _position()
    session = _FeedbackSession(position)
    service = FeedbackService(cast(Any, _FeedbackSessionMaker(session)))

    created = await service.create(
        position.id,
        PositionFeedbackCreate(reason="confirmed_opportunity"),
    )

    assert created is not None
    assert created.dimension == "opportunity"
    assert created.value == "yes"
    assert created.source_family_keys == [
        "host:example.test|template:example.test/jobs/{id}"
    ]
    assert session.feedback is not None
    assert session.feedback.context_snapshot == {
        "position_url": "https://example.test/jobs/7",
        "listing_page_id": None,
        "listing_url": None,
        "schema_fingerprint": None,
        "last_seen_at": None,
    }


async def test_feedback_for_missing_position_is_not_created() -> None:
    session = _FeedbackSession(None)
    service = FeedbackService(cast(Any, _FeedbackSessionMaker(session)))

    result = await service.create(
        999,
        PositionFeedbackCreate(reason="broken_link"),
    )

    assert result is None
    assert session.feedback is None
    assert session.commits == 0


def test_feedback_routes_are_exposed_in_openapi(client) -> None:
    schema = client.get("/openapi.json").json()
    create_path = schema["paths"]["/v1/positions/{position_id}/feedback"]
    retract_path = schema["paths"][
        "/v1/positions/{position_id}/feedback/{feedback_id}/retract"
    ]
    assert set(create_path) == {"post"}
    assert create_path["post"]["responses"]["201"]
    assert set(retract_path) == {"post"}
