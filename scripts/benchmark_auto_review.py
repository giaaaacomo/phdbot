"""Read-only smoke benchmark for the automatic-review tool against real candidates."""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from phd_searcher.config import Settings
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.pipeline.auto_review import _accepted_status, _native_ollama_review


async def benchmark(limit: int) -> None:
    settings = Settings()
    engine = create_async_engine(settings.database.url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT p.id, p.title, p.url, p.description, p.full_description,
                           p.position_type, p.institution_name, p.institution_country,
                           u.name AS university_name, u.country AS university_country
                    FROM positions p
                    LEFT JOIN universities u ON u.id = p.university_id
                    WHERE p.screening_status = 'review'
                      AND p.screening_manual IS FALSE
                      AND p.is_active IS TRUE
                      AND (p.deadline IS NULL OR p.deadline >= CURRENT_DATE)
                    ORDER BY p.id
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
            rows = result.mappings().all()
    finally:
        await engine.dispose()

    candidates: list[tuple[Position, University | None]] = []
    for row in rows:
        position = Position(
            id=row["id"],
            title=row["title"],
            url=row["url"],
            description=row["description"] or "",
            full_description=row["full_description"],
            position_type=row["position_type"],
            institution_name=row["institution_name"],
            institution_country=row["institution_country"],
        )
        university = None
        if row["university_name"]:
            university = University(
                wikidata_id=f"benchmark:{row['id']}",
                name=row["university_name"],
                country=row["university_country"],
                website_url="",
            )
        candidates.append((position, university))

    decisions = await _native_ollama_review(settings, candidates)
    print(
        json.dumps(
            [
                {
                    **decision.model_dump(),
                    "accepted_status": _accepted_status(decision),
                }
                for decision in decisions
            ],
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=4)
    args = parser.parse_args()
    asyncio.run(benchmark(args.limit))


if __name__ == "__main__":
    main()
