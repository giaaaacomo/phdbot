"""Auditable user feedback about a searchable position."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phd_searcher.database.models.base import Base


class PositionFeedback(Base):
    """One reversible report; it never changes or deletes the position itself."""

    __tablename__ = "position_feedback"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('confirmed_opportunity', 'non_opportunity', 'closed', 'duplicate', 'wrong_type', "
            "'mismatched_details', 'broken_link', 'other')",
            name="ck_position_feedback_reason",
        ),
        CheckConstraint(
            "status IN ('open', 'retracted')",
            name="ck_position_feedback_status",
        ),
        Index(
            "uq_position_feedback_open_dimension",
            "position_id",
            "dimension",
            unique=True,
            postgresql_where=text("status = 'open' AND dimension IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(
        ForeignKey("positions.id", ondelete="RESTRICT"),
        index=True,
    )
    reason: Mapped[str] = mapped_column(String(32), index=True)
    dimension: Mapped[str | None] = mapped_column(String(32), index=True)
    value: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    source_family_version: Mapped[str | None] = mapped_column(String(64))
    source_family_keys: Mapped[list[str] | None] = mapped_column(JSONB)
    context_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    retracted_at: Mapped[datetime | None]
