"""Persistent, reversible feedback without automatic global side effects."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast

from injector import inject
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.position_feedback import PositionFeedback
from phd_searcher.pipeline.source_family import SOURCE_FAMILY_VERSION, source_family_keys
from phd_searcher.typedef.feedback import (
    FeedbackDimension,
    FeedbackReason,
    FeedbackStatus,
    PositionFeedbackCreate,
    PositionFeedbackView,
)

_FEEDBACK_SEMANTICS: dict[FeedbackReason, tuple[FeedbackDimension, str]] = {
    "confirmed_opportunity": ("opportunity", "yes"),
    "non_opportunity": ("opportunity", "no"),
    "closed": ("availability", "closed"),
    "duplicate": ("duplicate", "yes"),
    "wrong_type": ("type", "wrong"),
    "mismatched_details": ("extraction", "mismatched"),
    "broken_link": ("reachability", "broken"),
    "other": ("other", "reported"),
}


def _schema_fingerprint(schema: dict[str, object] | None) -> str | None:
    if not schema:
        return None
    serialized = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(serialized.encode()).hexdigest()


class FeedbackService:
    @inject
    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    @staticmethod
    def _view(feedback: PositionFeedback) -> PositionFeedbackView:
        reason = cast(FeedbackReason, feedback.reason)
        fallback_dimension, fallback_value = _FEEDBACK_SEMANTICS[reason]
        return PositionFeedbackView(
            id=feedback.id,
            position_id=feedback.position_id,
            reason=reason,
            dimension=cast(
                FeedbackDimension,
                feedback.dimension or fallback_dimension,
            ),
            value=feedback.value or fallback_value,
            note=feedback.note,
            source_family_version=feedback.source_family_version,
            source_family_keys=list(feedback.source_family_keys or []),
            status=cast(FeedbackStatus, feedback.status),
            created_at=feedback.created_at,
            retracted_at=feedback.retracted_at,
        )

    async def create(
        self,
        position_id: int,
        body: PositionFeedbackCreate,
    ) -> PositionFeedbackView | None:
        """Append a report without changing position or index state."""
        async with self._session_maker() as session:
            position = await session.get(Position, position_id)
            if position is None:
                return None
            listing_page = (
                await session.get(ListingPage, position.listing_page_id)
                if position.listing_page_id is not None
                else None
            )
            dimension, value = _FEEDBACK_SEMANTICS[body.reason]
            now = datetime.now(UTC).replace(tzinfo=None)
            # One active operator label per position and semantic dimension.
            # Replacing it is append-preserving: the previous row is retracted.
            await session.execute(
                update(PositionFeedback)
                .where(
                    PositionFeedback.position_id == position_id,
                    PositionFeedback.dimension == dimension,
                    PositionFeedback.status == "open",
                )
                .values(status="retracted", retracted_at=now)
            )
            keys = source_family_keys(
                position.url,
                listing_url=listing_page.url if listing_page is not None else None,
                listing_page_id=position.listing_page_id,
            )
            feedback = PositionFeedback(
                position_id=position_id,
                reason=body.reason,
                dimension=dimension,
                value=value,
                note=body.note,
                status="open",
                source_family_version=SOURCE_FAMILY_VERSION,
                source_family_keys=list(keys),
                context_snapshot={
                    "position_url": position.url,
                    "listing_page_id": position.listing_page_id,
                    "listing_url": listing_page.url if listing_page is not None else None,
                    "schema_fingerprint": _schema_fingerprint(
                        listing_page.extraction_schema if listing_page is not None else None
                    ),
                    "last_seen_at": (
                        position.scraped_at.replace(tzinfo=UTC).isoformat()
                        if position.scraped_at is not None
                        else None
                    ),
                },
            )
            session.add(feedback)
            await session.commit()
            await session.refresh(feedback)
            return self._view(feedback)

    async def retract(
        self,
        position_id: int,
        feedback_id: int,
    ) -> PositionFeedbackView | None:
        """Record an undo while preserving the original report for audit."""
        async with self._session_maker() as session:
            feedback = await session.get(PositionFeedback, feedback_id)
            if feedback is None or feedback.position_id != position_id:
                return None
            if feedback.status != "retracted":
                feedback.status = "retracted"
                feedback.retracted_at = datetime.now(UTC).replace(tzinfo=None)
                await session.commit()
            return self._view(feedback)
