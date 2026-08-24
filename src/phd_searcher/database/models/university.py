"""University row: anagrafica + stato pipeline (discovery, schema estrazione)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from phd_searcher.database.models.base import Base


class University(Base):
    __tablename__ = "universities"

    id: Mapped[int] = mapped_column(primary_key=True)
    wikidata_id: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(512))
    country: Mapped[str] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    website_url: Mapped[str] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(Text)
    spontaneous_application_url: Mapped[str | None] = mapped_column(String(2048))
    # core = tassonomia universitaria; specialist = istituto superiore ammesso
    # tramite classi mirate e segnali di riconoscimento accademico.
    catalog_tier: Mapped[str] = mapped_column(String(16), default="core")
    catalog_basis: Mapped[str] = mapped_column(String(128), default="wikidata:Q3918")
    # ponytail: n. di sitelink Wikipedia come proxy di notorietà; QS/THE se serve un ranking vero
    sitelinks: Mapped[int] = mapped_column(default=0)
    # pending | done | no_listing | failed
    discovery_status: Mapped[str] = mapped_column(String(16), default="pending")
    discovery_checked_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
