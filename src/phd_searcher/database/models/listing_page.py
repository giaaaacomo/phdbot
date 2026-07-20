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
    schema_status: Mapped[str] = mapped_column(String(16), default="missing")  # missing | ok | stale | failed
    # se valorizzato (es. "page"), lo scrape pagina ?param=0..N finché una pagina non dà item
    pagination_param: Mapped[str | None] = mapped_column(String(16))
    last_scraped_at: Mapped[datetime | None]
    discovered_at: Mapped[datetime] = mapped_column(server_default=func.now())
