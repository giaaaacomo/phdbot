"""Pagina che elenca bandi PhD: N per ateneo, o aggregatore (university_id NULL)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phd_searcher.database.models.base import Base


class ListingPage(Base):
    __tablename__ = "listing_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    university_id: Mapped[int | None] = mapped_column(ForeignKey("universities.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(String(2048), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default="university")  # university | aggregator
    source: Mapped[str] = mapped_column(String(16), default="funnel")  # funnel | search | seed
    extraction_schema: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    # missing | ok | stale | failed | unsupported (PDF/DOCX/...)
    schema_status: Mapped[str] = mapped_column(String(16), default="missing")
    # se valorizzato (es. "page"), lo scrape pagina ?param=0..N finché una pagina non dà item
    pagination_param: Mapped[str | None] = mapped_column(String(16))
    # Il quality gate non elimina una sorgente: la mette in quarantena e
    # conserva metriche sufficienti per correggere/rigenerare lo schema.
    quality_status: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    quality_reason: Mapped[str | None] = mapped_column(String(256))
    quality_metrics: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    quality_checked_at: Mapped[datetime | None]
    last_scraped_at: Mapped[datetime | None]
    discovered_at: Mapped[datetime] = mapped_column(server_default=func.now())
