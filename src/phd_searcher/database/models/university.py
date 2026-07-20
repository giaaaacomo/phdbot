"""University row: anagrafica + stato pipeline (discovery, schema estrazione)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column

from phd_searcher.database.models.base import Base


class University(Base):
    __tablename__ = "universities"

    id: Mapped[int] = mapped_column(primary_key=True)
    wikidata_id: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(512))
    country: Mapped[str] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    website_url: Mapped[str] = mapped_column(String(2048))
    # ponytail: n. di sitelink Wikipedia come proxy di notorietà; QS/THE se serve un ranking vero
    sitelinks: Mapped[int] = mapped_column(default=0)
    # pending | done | no_listing | failed
    discovery_status: Mapped[str] = mapped_column(String(16), default="pending")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
