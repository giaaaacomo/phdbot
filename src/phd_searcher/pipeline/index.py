"""Stadio 5: embedding (litellm) + upsert in Qdrant; rimozione bandi scaduti."""

from __future__ import annotations

from datetime import UTC, date, datetime

from injector import Injector
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.config.qdrant import QdrantConfig
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.engine.model_helper import ModelHelper
from phd_searcher.pipeline.progress import Progress

_BATCH = 64


async def _ensure_collection(qdrant: AsyncQdrantClient, name: str, dim: int) -> None:
    if not await qdrant.collection_exists(name):
        await qdrant.create_collection(name, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    """Ritorna il numero di posizioni indicizzate. name_like ignorato: indicizza tutto il nuovo."""
    progress = progress or Progress()
    qdrant = container.get(AsyncQdrantClient)
    config = container.get(QdrantConfig)
    model = container.get(ModelHelper)
    session_maker = container.get(async_sessionmaker[AsyncSession])
    today = date.today()

    async with session_maker() as session:
        # 1) rimuovi dall'indice i bandi scaduti
        expired_stmt = select(Position).where(
            Position.deadline.is_not(None),
            Position.deadline < today,
            Position.indexed_at.is_not(None),
        )
        expired = (await session.execute(expired_stmt)).scalars().all()
        if expired and await qdrant.collection_exists(config.collection):
            await qdrant.delete(config.collection, points_selector=[p.id for p in expired])
            for p in expired:
                p.indexed_at = None
            await session.commit()
            print(f"index: removed {len(expired)} expired positions")
        # ponytail: i bandi senza deadline spariti dal sito restano indicizzati per sempre;
        # aggiungere eviction per "non più visto da N run" se diventa un problema

        # 2) indicizza le posizioni attive non ancora indicizzate
        stmt = (
            select(Position, University)
            .outerjoin(University, Position.university_id == University.id)
            .where(Position.indexed_at.is_(None))
            .where((Position.deadline.is_(None)) | (Position.deadline >= today))
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await session.execute(stmt)).all()

        total = 0
        await progress.begin((len(rows) + _BATCH - 1) // _BATCH)
        for start in range(0, len(rows), _BATCH):
            if progress.should_stop:
                break
            await progress.tick(f"batch {start // _BATCH + 1}")
            batch = rows[start : start + _BATCH]
            vectors = await model.embed([f"{p.title}\n{p.description}" for p, _ in batch])
            await _ensure_collection(qdrant, config.collection, dim=len(vectors[0]))
            points = [
                PointStruct(
                    id=p.id,
                    vector=vec,
                    payload={
                        "title": p.title,
                        "url": p.url,
                        "university": u.name if u else (p.institution_name or ""),
                        "country": u.country if u else (p.institution_country or ""),
                        "deadline": p.deadline.isoformat() if p.deadline else None,
                    },
                )
                for (p, u), vec in zip(batch, vectors, strict=True)
            ]
            await qdrant.upsert(config.collection, points=points)
            now = datetime.now(UTC).replace(tzinfo=None)  # colonna naive: asyncpg rifiuta datetime tz-aware
            for p, _ in batch:
                p.indexed_at = now
            await session.commit()
            total += len(points)
        print(f"index: upserted {total} positions")
    return total
