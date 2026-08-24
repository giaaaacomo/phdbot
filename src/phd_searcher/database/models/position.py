"""Open PhD position, normalizzata nello schema unico."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from phd_searcher.database.models.base import Base
from phd_searcher.opportunity_kinds import DEFAULT_OPPORTUNITY_KIND, OpportunityKind


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
    duration_raw: Mapped[str | None] = mapped_column(String(256))
    compensation_raw: Mapped[str | None] = mapped_column(String(512))
    compensation_min: Mapped[float | None] = mapped_column(Float)
    compensation_max: Mapped[float | None] = mapped_column(Float)
    compensation_currency: Mapped[str | None] = mapped_column(String(3))
    compensation_period: Mapped[str | None] = mapped_column(String(16))
    published_raw: Mapped[str | None] = mapped_column(String(256))
    published_at: Mapped[date | None]
    position_type: Mapped[str] = mapped_column(String(32), default="other")
    opportunity_kind: Mapped[OpportunityKind] = mapped_column(
        String(32),
        default=DEFAULT_OPPORTUNITY_KIND,
        server_default=DEFAULT_OPPORTUNITY_KIND,
    )
    # Pre-screening reversibile: review/rejected restano nel DB e possono
    # essere promossi manualmente senza ripetere scrape o discovery.
    screening_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    screening_reason: Mapped[str | None] = mapped_column(String(256))
    screening_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    screening_source: Mapped[str] = mapped_column(String(32), default="rules")
    # Verdetto grezzo prima dell'applicazione delle soglie conservative.
    screening_decision: Mapped[str | None] = mapped_column(String(16))
    screening_confidence: Mapped[float | None] = mapped_column(Float)
    screening_evidence: Mapped[str | None] = mapped_column(Text)
    screening_model: Mapped[str | None] = mapped_column(String(128))
    screening_version: Mapped[str | None] = mapped_column(String(64))
    screened_at: Mapped[datetime | None]
    # Stato operativo della cascata di review. E' separato dal verdetto
    # pubblico (screening_status): una riga puo' restare ``review`` ma avere
    # bisogno di un fetch, di una review profonda o di un intervento tecnico.
    review_state: Mapped[str] = mapped_column(String(32), default="untriaged", index=True)
    routing_reason: Mapped[str | None] = mapped_column(String(256))
    research_group: Mapped[str | None] = mapped_column(String(512))
    full_description: Mapped[str | None] = mapped_column(Text)
    details_scraped_at: Mapped[datetime | None]
    deadline_raw: Mapped[str | None] = mapped_column(String(256))
    deadline: Mapped[date | None]
    # Un bando diventa inattivo solo dopo due scansioni complete consecutive in cui
    # non compare più nella propria sorgente. I test parziali non incrementano il contatore.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    missing_runs: Mapped[int] = mapped_column(Integer, default=0)
    # `first_seen_at` is immutable acquisition provenance. Legacy rows remain
    # nullable rather than receiving a fabricated migration timestamp.
    first_seen_at: Mapped[datetime | None] = mapped_column(server_default=func.now())
    # Historical DB/API name retained for compatibility: this is updated only
    # by the scrape upsert and means "last seen by PHDBOT".
    scraped_at: Mapped[datetime] = mapped_column(server_default=func.now())
    indexed_at: Mapped[datetime | None]  # None = da (ri)indicizzare in Qdrant
