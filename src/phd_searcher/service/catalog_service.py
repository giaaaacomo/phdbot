"""Lettura da Postgres: dettaglio posizione e stato copertura per ateneo."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

from injector import inject
from qdrant_client import AsyncQdrantClient
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.clock import local_today
from phd_searcher.config.qdrant import QdrantConfig
from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.review_attempt import ReviewAttempt
from phd_searcher.database.models.university import University
from phd_searcher.opportunity_kinds import normalize_opportunity_kind
from phd_searcher.pipeline.review_audit import append_review_attempt
from phd_searcher.position_types import POSITION_TYPES, classify_position
from phd_searcher.typedef.search import (
    CoverageResult,
    PositionDetail,
    PositionLookup,
    ReviewAttemptItem,
    ScreeningItem,
    ScreeningPage,
    ScreeningStatus,
    ScreeningUpdate,
    SearchFacetInstitution,
    SearchFacets,
    UniversityCoverage,
)


class CatalogService:
    @inject
    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        qdrant: AsyncQdrantClient,
        qdrant_config: QdrantConfig,
    ) -> None:
        self._session_maker = session_maker
        self._qdrant = qdrant
        self._qdrant_config = qdrant_config

    @staticmethod
    def _screening_item(position: Position, university: University | None) -> ScreeningItem:
        return ScreeningItem(
            id=position.id,
            title=position.title,
            url=position.url,
            description=position.full_description or position.description,
            position_type=position.position_type,
            opportunity_kind=normalize_opportunity_kind(position.opportunity_kind),
            status=cast(ScreeningStatus, position.screening_status),
            reason=position.screening_reason,
            manual=position.screening_manual,
            source=position.screening_source,
            decision=cast(ScreeningStatus, position.screening_decision) if position.screening_decision else None,
            confidence=position.screening_confidence,
            evidence=(
                list(json.loads(position.screening_evidence))
                if position.screening_evidence
                else []
            ),
            model=position.screening_model,
            version=position.screening_version,
            screened_at=position.screened_at,
            review_state=position.review_state,
            routing_reason=position.routing_reason,
            university=university.name if university else (position.institution_name or ""),
            country=university.country if university else (position.institution_country or ""),
        )

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
                    description=p.full_description or p.description,
                    deadline=p.deadline,
                    deadline_raw=p.deadline_raw,
                    published_raw=p.published_raw,
                    published_at=p.published_at,
                    first_seen_at=p.first_seen_at,
                    last_seen_at=p.scraped_at,
                    scraped_at=p.scraped_at,
                    duration=p.duration_raw,
                    compensation=p.compensation_raw,
                    compensation_min=p.compensation_min,
                    compensation_max=p.compensation_max,
                    compensation_currency=p.compensation_currency,
                    compensation_period=p.compensation_period,
                    position_type=classify_position(p.title, p.full_description or p.description, p.position_type),
                    opportunity_kind=normalize_opportunity_kind(p.opportunity_kind),
                    university=u.name if u else (p.institution_name or ""),
                    country=u.country if u else (p.institution_country or ""),
                ),
            )

    async def screening(
        self,
        status: ScreeningStatus | None,
        *,
        limit: int,
        offset: int,
    ) -> ScreeningPage:
        current_filter = (
            Position.is_active.is_(True),
            or_(Position.deadline.is_(None), Position.deadline >= local_today()),
        )
        async with self._session_maker() as session:
            counts_rows = (
                await session.execute(
                    select(Position.screening_status, func.count(Position.id))
                    .where(*current_filter)
                    .group_by(Position.screening_status)
                )
            ).all()
            stmt = (
                select(Position, University)
                .outerjoin(University, Position.university_id == University.id)
                .where(*current_filter)
                .order_by(Position.screened_at.desc().nulls_last(), Position.id.desc())
                .offset(offset)
                .limit(limit)
            )
            count_stmt = select(func.count(Position.id)).where(*current_filter)
            if status is not None:
                stmt = stmt.where(Position.screening_status == status)
                count_stmt = count_stmt.where(Position.screening_status == status)
            rows = (await session.execute(stmt)).all()
            total = int((await session.execute(count_stmt)).scalar_one())
        return ScreeningPage(
            items=[self._screening_item(position, university) for position, university in rows],
            total=total,
            counts={str(key): int(value) for key, value in counts_rows},
            limit=limit,
            offset=offset,
        )

    async def update_screening(self, position_id: int, body: ScreeningUpdate) -> ScreeningItem | None:
        was_indexed = False
        async with self._session_maker() as session:
            row = (
                await session.execute(
                    select(Position, University)
                    .outerjoin(University, Position.university_id == University.id)
                    .where(Position.id == position_id)
                )
            ).first()
            if row is None:
                return None
            position, university = row
            was_indexed = position.indexed_at is not None
            position.screening_status = body.status
            if body.status == "eligible" and position.opportunity_kind == "unknown":
                # Manual approval represents a concrete search result unless a
                # prior automatic review already identified a programme or a
                # spontaneous route.
                position.opportunity_kind = "vacancy"
            position.screening_reason = f"manual:{body.reason.strip()}" if body.reason else "manual_override"
            position.screening_manual = True
            position.screening_source = "manual"
            position.screening_decision = body.status
            position.screening_confidence = 1.0
            position.screening_evidence = None
            position.screening_model = None
            position.screening_version = None
            position.screened_at = datetime.now(UTC).replace(tzinfo=None)
            position.review_state = "resolved" if body.status != "review" else "human_review"
            position.routing_reason = position.screening_reason
            position.indexed_at = None
            append_review_attempt(
                session,
                position_id=position.id,
                pipeline_run_id=None,
                stage="manual",
                model=None,
                version="manual-v1",
                raw_decision=body.status,
                accepted_status=body.status,
                position_type=position.position_type,
                confidence=1.0,
                evidence=[],
                reason=position.screening_reason,
                details={"review_state": position.review_state},
            )
            await session.commit()
            item = self._screening_item(position, university)
        if was_indexed and await self._qdrant.collection_exists(self._qdrant_config.collection):
            await self._qdrant.delete(
                self._qdrant_config.collection,
                points_selector=[position_id],
                wait=True,
            )
        return item

    async def review_attempts(self, position_id: int) -> list[ReviewAttemptItem]:
        async with self._session_maker() as session:
            rows = (
                await session.execute(
                    select(ReviewAttempt)
                    .where(ReviewAttempt.position_id == position_id)
                    .order_by(ReviewAttempt.created_at.desc(), ReviewAttempt.id.desc())
                )
            ).scalars().all()
        return [
            ReviewAttemptItem(
                id=row.id,
                stage=row.stage,
                model=row.model,
                version=row.version,
                raw_decision=row.raw_decision,
                accepted_status=row.accepted_status,
                position_type=row.position_type,
                confidence=row.confidence,
                evidence=list(row.evidence),
                reason=row.reason,
                tool_attempts=row.tool_attempts,
                latency_seconds=row.latency_seconds,
                details=dict(row.details),
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def search_facets(self) -> SearchFacets:
        async with self._session_maker() as session:
            rows = (
                await session.execute(
                    select(Position, University)
                    .outerjoin(University, Position.university_id == University.id)
                    .where(Position.indexed_at.is_not(None), Position.is_active.is_(True))
                )
            ).all()
        institutions = {
            (
                u.name if u else (position.institution_name or ""),
                u.country if u else (position.institution_country or ""),
            )
            for position, u in rows
        }
        clean = sorted((name, country) for name, country in institutions if name and country)
        return SearchFacets(
            countries=sorted({country for _, country in clean}),
            institutions=[SearchFacetInstitution(name=name, country=country) for name, country in clean],
            position_types=POSITION_TYPES,
        )

    async def coverage(self) -> CoverageResult:
        async with self._session_maker() as session:
            pages_count = func.count(func.distinct(ListingPage.id))
            pages_ok = func.count(func.distinct(ListingPage.id)).filter(ListingPage.schema_status == "ok")
            pages_quarantined = func.count(func.distinct(ListingPage.id)).filter(
                ListingPage.quality_status == "quarantine"
            )
            positions_count = func.count(func.distinct(Position.id)).filter(Position.is_active.is_(True))
            positions_quarantined = func.count(func.distinct(Position.id)).filter(
                Position.is_active.is_(True),
                Position.screening_status == "quarantine",
            )
            rows = (
                await session.execute(
                    select(
                        University,
                        pages_count,
                        pages_ok,
                        pages_quarantined,
                        positions_count,
                        positions_quarantined,
                    )
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
                        catalog_tier=u.catalog_tier,
                        catalog_basis=u.catalog_basis,
                        listing_pages_count=pc,
                        listing_pages_ok=ok,
                        listing_pages_quarantined=pq,
                        positions_count=n,
                        positions_quarantined=nq,
                    )
                    for u, pc, ok, pq, n, nq in rows
                ]
            )
