"""Read-only audit of current records excluded from the semantic index."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from injector import Injector
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.clock import local_today
from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.pipeline.index import (
    _POSITION_INDEX_KINDS,
    _PROVISIONAL_INDEX_KINDS,
    _PROVISIONAL_SCREENING_STATUSES,
    _has_authoritative_verified_status,
    _provisional_gate_decision,
)


def _sample(position: Position, listing_page: ListingPage | None) -> dict[str, Any]:
    return {
        "id": position.id,
        "title": position.title,
        "screening_status": position.screening_status,
        "position_type": position.position_type,
        "opportunity_kind": position.opportunity_kind,
        "review_state": position.review_state,
        "source": listing_page.source if listing_page is not None else None,
        "schema_status": listing_page.schema_status if listing_page is not None else None,
        "quality_status": listing_page.quality_status if listing_page is not None else None,
    }


def _source_group(listing_page: ListingPage | None) -> str:
    if listing_page is None:
        return "no_listing"
    try:
        host = urlsplit(listing_page.url).hostname or "invalid_host"
    except ValueError:
        host = "invalid_host"
    return f"{listing_page.source}:{host}"


async def audit(
    container: Injector,
    *,
    today: date | None = None,
    samples_per_reason: int = 3,
) -> dict[str, Any]:
    """Aggregate deterministic gate outcomes without model, Qdrant, or writes."""

    current_day = today or local_today()
    session_maker = container.get(async_sessionmaker[AsyncSession])
    reasons: Counter[str] = Counter()
    by_reason_type: dict[str, Counter[str]] = defaultdict(Counter)
    by_reason_status: dict[str, Counter[str]] = defaultdict(Counter)
    by_reason_source: dict[str, Counter[str]] = defaultdict(Counter)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    searchable = 0
    total = 0

    statement = (
        select(Position, ListingPage)
        .outerjoin(ListingPage, Position.listing_page_id == ListingPage.id)
        .where(Position.indexed_at.is_(None))
        .where(Position.is_active.is_(True))
        .where(or_(Position.deadline.is_(None), Position.deadline >= current_day))
        .where(Position.screening_status.in_(_PROVISIONAL_SCREENING_STATUSES))
        .where(Position.opportunity_kind.in_(_PROVISIONAL_INDEX_KINDS))
        .order_by(Position.id)
        .execution_options(yield_per=500)
    )
    async with session_maker() as session:
        rows = await session.stream(statement)
        async for position, listing_page in rows:
            total += 1
            if (
                position.screening_status == "eligible"
                and position.opportunity_kind in _POSITION_INDEX_KINDS
                and _has_authoritative_verified_status(position, today=current_day)
            ):
                reason = "searchable_verified"
                searchable += 1
            else:
                decision = _provisional_gate_decision(
                    position,
                    listing_page=listing_page,
                    today=current_day,
                )
                reason = decision.reason
                if decision.assessment is not None:
                    reason = f"searchable_{reason}"
                    searchable += 1
            reasons[reason] += 1
            by_reason_type[reason][position.position_type] += 1
            by_reason_status[reason][position.screening_status] += 1
            by_reason_source[reason][_source_group(listing_page)] += 1
            if len(samples[reason]) < samples_per_reason:
                samples[reason].append(_sample(position, listing_page))

    return {
        "as_of": current_day.isoformat(),
        "scope": "active, current, coarse-index-candidate, indexed_at IS NULL",
        "total": total,
        "searchable_without_reconciliation": searchable,
        "excluded": total - searchable,
        "reasons": [
            {
                "reason": reason,
                "count": count,
                "position_types": dict(by_reason_type[reason].most_common()),
                "screening_statuses": dict(by_reason_status[reason].most_common()),
                "top_sources": dict(by_reason_source[reason].most_common(15)),
                "samples": samples[reason],
            }
            for reason, count in reasons.most_common()
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Explain why current PHDBOT records are absent from search",
    )
    parser.add_argument(
        "--samples-per-reason",
        type=int,
        default=3,
        choices=range(11),
    )
    args = parser.parse_args()

    load_dotenv()
    from phd_searcher.dependency import container

    report = asyncio.run(
        audit(
            container,
            samples_per_reason=args.samples_per_reason,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
