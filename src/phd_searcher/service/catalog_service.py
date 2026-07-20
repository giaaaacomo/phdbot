"""Lettura da Postgres: dettaglio posizione e stato copertura per ateneo."""

from __future__ import annotations

from injector import inject
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.typedef.search import CoverageResult, PositionDetail, PositionLookup, UniversityCoverage


class CatalogService:
    @inject
    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    async def position(self, position_id: int) -> PositionLookup:
        async with self._session_maker() as session:
            row = (
                await session.execute(
                    select(Position, University)
                    .outerjoin(University, Position.university_id == University.id)
                    .where(Position.id == position_id)
                )
            ).first()
            if row is None:
                return PositionLookup(found=False)
            p, u = row
            return PositionLookup(
                found=True,
                position=PositionDetail(
                    id=p.id,
                    title=p.title,
                    url=p.url,
                    description=p.description,
                    deadline=p.deadline,
                    deadline_raw=p.deadline_raw,
                    university=u.name if u else (p.institution_name or ""),
                    country=u.country if u else (p.institution_country or ""),
                ),
            )

    async def coverage(self) -> CoverageResult:
        async with self._session_maker() as session:
            pages_count = func.count(func.distinct(ListingPage.id))
            pages_ok = func.count(func.distinct(ListingPage.id)).filter(ListingPage.schema_status == "ok")
            positions_count = func.count(func.distinct(Position.id))
            rows = (
                await session.execute(
                    select(University, pages_count, pages_ok, positions_count)
                    .outerjoin(ListingPage, ListingPage.university_id == University.id)
                    .outerjoin(Position, Position.university_id == University.id)
                    .group_by(University.id)
                    .order_by(University.country, University.name)
                )
            ).all()
            return CoverageResult(
                universities=[
                    UniversityCoverage(
                        name=u.name,
                        country=u.country,
                        website_url=u.website_url,
                        discovery_status=u.discovery_status,
                        listing_pages_count=pc,
                        listing_pages_ok=ok,
                        positions_count=n,
                    )
                    for u, pc, ok, n in rows
                ]
            )
