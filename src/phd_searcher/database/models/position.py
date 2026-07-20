"""Open PhD position, normalizzata nello schema unico."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from phd_searcher.database.models.base import Base


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    university_id: Mapped[int | None] = mapped_column(ForeignKey("universities.id", ondelete="CASCADE"))
    listing_page_id: Mapped[int | None] = mapped_column(ForeignKey("listing_pages.id", ondelete="SET NULL"))
    # valorizzati solo per item da aggregatori non riconducibili a un ateneo censito
    institution_name: Mapped[str | None] = mapped_column(String(512))
    institution_country: Mapped[str | None] = mapped_column(String(2))
    url: Mapped[str] = mapped_column(String(2048), unique=True)
    title: Mapped[str] = mapped_column(String(1024))
    description: Mapped[str] = mapped_column(Text, default="")
    area: Mapped[str | None] = mapped_column(String(256))  # research area/field, se esposta dal sito
    language: Mapped[str | None] = mapped_column(String(8))  # lingua del bando, se esposta
    deadline_raw: Mapped[str | None] = mapped_column(String(256))
    deadline: Mapped[date | None]
    scraped_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    indexed_at: Mapped[datetime | None]  # None = da (ri)indicizzare in Qdrant
