"""Read-only shadow benchmark for PHDBOT embedding strategies.

The benchmark reads the IDs currently present in the production Qdrant
collection (or, optionally, rows marked as indexed in PostgreSQL), fetches the
corresponding candidate fields from PostgreSQL, and performs all embedding and
ranking locally.  It never creates, updates, or deletes database/Qdrant data.

Examples::

    uv run python scripts/benchmark_embeddings.py --limit 500 --batch 32
    uv run python scripts/benchmark_embeddings.py \
        --model nomic-embed-text::raw \
        --model nomic-embed-text::nomic \
        --model qwen3-embedding:0.6b::qwen \
        --format markdown --output /tmp/embedding-benchmark.md
    uv run python scripts/benchmark_embeddings.py \
        --model nomic-embed-text::nomic \
        --document-variant compact \
        --document-variant compact-area

Model profiles are separated from Ollama model tags with ``::``:

* ``raw``: current unprefixed query/document behaviour;
* ``nomic``: Nomic's ``search_query:`` / ``search_document:`` contract;
* ``qwen``: Qwen3's instructed-query retrieval contract (documents are raw).

The bundled gold set is deliberately a small, positive-only recall canary. It
can detect lost known-relevant opportunities, but unjudged documents are
unknown rather than negative: neither its ranking metrics nor threshold return
counts estimate precision without a separate pooled manual audit.
"""

from __future__ import annotations

import argparse
import asyncio
import heapq
import json
import math
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from dotenv import load_dotenv
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.sql import Select

from phd_searcher.config import Settings
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.university import University
from phd_searcher.engine.search_documents import (
    build_candidate_search_document,
    clean_search_text,
)
from phd_searcher.service.search_service import normalize_retrieval_query

Profile = Literal["raw", "nomic", "qwen"]
DocumentVariant = Literal["compact", "compact-area"]
GoldJudgments = dict[str, dict[int, int]]

GOLD_SCHEMA_VERSION = 1
METRICS_CUTOFF = 20
DEFAULT_SCORE_THRESHOLDS = (0.45, 0.50, 0.55, 0.60, 0.65, 0.70)
DEFAULT_GOLD_SET = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "embedding_relevance_gold.v1.json"
)

DEFAULT_QUERIES = (
    "biodesign",
    "xr",
    "extended reality",
    "vr",
    "virtual reality",
    "spatial computing",
    "interaction design",
    "realtà estesa",
    "realtà virtuale",
    "calcolo spaziale",
    "design dell'interazione",
    "progettazione dell'interazione",
    "ingegneria navale",
)

QWEN_RETRIEVAL_INSTRUCTION = (
    "Given a search query about academic opportunities, retrieve relevant "
    "academic job and study opportunity descriptions"
)


@dataclass(frozen=True, slots=True)
class Candidate:
    position_id: int
    title: str
    position_type: str
    institution: str
    description: str
    area: str | None = None
    full_description: str | None = None


@dataclass(frozen=True, slots=True)
class ModelSpec:
    model: str
    profile: Profile

    @property
    def label(self) -> str:
        return f"{self.model}::{self.profile}"


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    rank: int
    score: float
    position_id: int
    title: str
    position_type: str
    institution: str


@dataclass(frozen=True, slots=True)
class Corpus:
    candidates: list[Candidate]
    indexed_ids: int
    extra_ids_requested: list[int]
    missing_ids: list[int]


def clean_text(value: str | None, *, max_chars: int = 0) -> str:
    """Compatibility wrapper around the production search-text cleaner."""
    return clean_search_text(value, max_chars=max_chars)


def build_candidate_document(
    candidate: Candidate,
    *,
    description_chars: int = 800,
    include_full_description: bool = False,
    include_area: bool = False,
) -> str:
    """Build a compact document containing candidate-specific evidence only.

    ``include_area`` exists only for an explicit benchmark A/B. Production's
    document builder remains unchanged until the comparison justifies a change.
    """
    description_source = (
        candidate.full_description
        if include_full_description and candidate.full_description
        else candidate.description
    )
    document = build_candidate_search_document(
        title=candidate.title,
        position_type=candidate.position_type,
        institution=candidate.institution,
        description=description_source,
        description_chars=description_chars,
    )
    area = clean_search_text(candidate.area) if include_area else ""
    return f"{document}\nResearch area: {area}" if area else document


def parse_model_spec(value: str) -> ModelSpec:
    """Parse ``OLLAMA_MODEL[::PROFILE]`` without confusing Ollama ``:tag``."""
    model, separator, requested_profile = value.rpartition("::")
    if not separator:
        model = value
        lowered = value.lower()
        if "nomic" in lowered:
            requested_profile = "nomic"
        elif "qwen" in lowered:
            requested_profile = "qwen"
        else:
            requested_profile = "raw"
    model = model.removeprefix("ollama/").strip()
    profile = requested_profile.strip().lower()
    if not model:
        raise argparse.ArgumentTypeError("model name cannot be empty")
    if profile not in {"raw", "nomic", "qwen"}:
        raise argparse.ArgumentTypeError(
            f"unsupported profile {profile!r}; expected raw, nomic, or qwen"
        )
    return ModelSpec(model=model, profile=profile)  # type: ignore[arg-type]


def format_document(document: str, profile: Profile) -> str:
    if profile == "nomic":
        return f"search_document: {document}"
    return document


def format_query(query: str, profile: Profile) -> str:
    if profile == "nomic":
        return f"search_query: {query}"
    if profile == "qwen":
        return f"Instruct: {QWEN_RETRIEVAL_INSTRUCTION}\nQuery: {query}"
    return query


def prepare_query_input(
    query: str,
    profile: Profile,
    *,
    normalize_retrieval: bool = True,
) -> tuple[str, str]:
    """Return the semantic query and exact model input used in production."""
    retrieval_query = (
        normalize_retrieval_query(query) if normalize_retrieval else query
    )
    return retrieval_query, format_query(retrieval_query, profile)


def normalize_vector(vector: Sequence[float]) -> tuple[float, ...]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("embedding is empty, non-finite, or has zero norm")
    return tuple(value / norm for value in vector)


def cosine_normalized(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError(f"embedding dimension mismatch: {len(left)} != {len(right)}")
    return sum(a * b for a, b in zip(left, right, strict=True))


def update_top_k(
    heap: list[tuple[float, int, Candidate]],
    *,
    score: float,
    candidate: Candidate,
    top_k: int,
) -> None:
    """Maintain a bounded min-heap; candidate ID makes ties deterministic."""
    item = (score, candidate.position_id, candidate)
    if len(heap) < top_k:
        heapq.heappush(heap, item)
    elif item[:2] > heap[0][:2]:
        heapq.heapreplace(heap, item)


def ranked_candidates(heap: Iterable[tuple[float, int, Candidate]]) -> list[RankedCandidate]:
    ordered = sorted(heap, key=lambda item: (item[0], item[1]), reverse=True)
    return [
        RankedCandidate(
            rank=rank,
            score=score,
            position_id=candidate.position_id,
            title=candidate.title,
            position_type=candidate.position_type,
            institution=candidate.institution,
        )
        for rank, (score, _, candidate) in enumerate(ordered, start=1)
    ]


def load_gold_set(path: Path) -> GoldJudgments:
    """Load and validate a small, versioned graded-relevance set."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != GOLD_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported gold-set schema {payload.get('schema_version')!r}; "
            f"expected {GOLD_SCHEMA_VERSION}"
        )
    raw_judgments = payload.get("judgments")
    if not isinstance(raw_judgments, dict) or not raw_judgments:
        raise ValueError("gold set must contain a non-empty judgments object")

    judgments: GoldJudgments = {}
    for query, raw_grades in raw_judgments.items():
        if not isinstance(query, str) or not query.strip() or not isinstance(raw_grades, dict):
            raise ValueError("each gold-set query must map to a grade object")
        query_judgments: dict[int, int] = {}
        for raw_grade, raw_ids in raw_grades.items():
            try:
                grade = int(raw_grade)
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid relevance grade {raw_grade!r} for {query!r}") from error
            if grade not in {1, 2} or not isinstance(raw_ids, list):
                raise ValueError(f"grades for {query!r} must be 1 or 2 with an ID list")
            for raw_id in raw_ids:
                if not isinstance(raw_id, int) or raw_id <= 0:
                    raise ValueError(f"invalid position ID {raw_id!r} for {query!r}")
                if raw_id in query_judgments:
                    raise ValueError(f"position ID {raw_id} has duplicate grades for {query!r}")
                query_judgments[raw_id] = grade
        if not query_judgments:
            raise ValueError(f"gold-set query {query!r} has no judged positions")
        judgments[query] = query_judgments
    return judgments


def gold_position_ids(judgments: GoldJudgments) -> list[int]:
    """Return all judged IDs in stable numeric order for corpus inclusion."""
    return sorted({position_id for grades in judgments.values() for position_id in grades})


def evaluate_ranking(
    position_ids: Sequence[int],
    judgments: dict[int, int],
    *,
    cutoff: int = METRICS_CUTOFF,
) -> dict[str, float | int | None]:
    """Calculate graded nDCG and grade-2 recall/MRR at a fixed depth."""
    ranked_ids = list(position_ids[:cutoff])
    gains = [(2 ** judgments.get(position_id, 0)) - 1 for position_id in ranked_ids]
    dcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, start=1))
    ideal_gains = sorted(((2**grade) - 1 for grade in judgments.values()), reverse=True)[:cutoff]
    ideal_dcg = sum(
        gain / math.log2(rank + 1) for rank, gain in enumerate(ideal_gains, start=1)
    )

    grade_2_ids = {position_id for position_id, grade in judgments.items() if grade == 2}
    grade_2_ranks = [
        rank
        for rank, position_id in enumerate(ranked_ids, start=1)
        if position_id in grade_2_ids
    ]
    return {
        f"ndcg_at_{cutoff}": dcg / ideal_dcg if ideal_dcg else None,
        f"grade_2_recall_at_{cutoff}": (
            len(set(ranked_ids) & grade_2_ids) / len(grade_2_ids) if grade_2_ids else None
        ),
        f"grade_2_mrr_at_{cutoff}": 1 / min(grade_2_ranks)
        if grade_2_ranks
        else (0.0 if grade_2_ids else None),
        "judged_positions": len(judgments),
        "grade_2_positions": len(grade_2_ids),
    }


def update_threshold_counts(
    counts: list[int],
    *,
    score: float,
    thresholds: Sequence[float] = DEFAULT_SCORE_THRESHOLDS,
) -> None:
    """Update exact corpus-wide result counts without retaining every score."""
    if len(counts) != len(thresholds):
        raise ValueError("threshold count and threshold lengths differ")
    for index, threshold in enumerate(thresholds):
        if score >= threshold:
            counts[index] += 1


def build_threshold_curve(
    *,
    thresholds: Sequence[float],
    returned_counts: Sequence[int],
    judgments: Mapping[int, int] | None = None,
    judged_scores: Mapping[int, float] | None = None,
) -> list[dict[str, float | int | None]]:
    """Build result-volume and known-positive recall diagnostics by threshold.

    ``returned`` is exact for the evaluated corpus. Grade-2 recall is only a
    recall canary because the bundled judgments do not label all results.
    """
    if len(returned_counts) != len(thresholds):
        raise ValueError("threshold count and threshold lengths differ")
    grade_2_ids = {
        position_id for position_id, grade in (judgments or {}).items() if grade == 2
    }
    scores = judged_scores or {}
    curve: list[dict[str, float | int | None]] = []
    for threshold, returned in zip(thresholds, returned_counts, strict=True):
        retrieved_grade_2 = sum(
            scores.get(position_id, -math.inf) >= threshold for position_id in grade_2_ids
        )
        curve.append(
            {
                "threshold": threshold,
                "returned": returned,
                "grade_2_retrieved": retrieved_grade_2 if grade_2_ids else None,
                "grade_2_recall": retrieved_grade_2 / len(grade_2_ids)
                if grade_2_ids
                else None,
            }
        )
    return curve


def build_judged_score_report(
    judgments: Mapping[int, int],
    judged_scores: Mapping[int, float],
) -> list[dict[str, float | int | None]]:
    """Expose every known judgment's raw score, including missing corpus IDs."""
    return [
        {
            "position_id": position_id,
            "grade": grade,
            "score": round(judged_scores[position_id], 6)
            if position_id in judged_scores
            else None,
        }
        for position_id, grade in sorted(
            judgments.items(), key=lambda item: (-item[1], item[0])
        )
    ]


def aggregate_threshold_curves(
    query_reports: Sequence[dict[str, Any]],
) -> list[dict[str, float | int | None]]:
    """Aggregate exact result volumes and macro known-positive recall."""
    curves = [report["threshold_curve"] for report in query_reports]
    if not curves:
        return []
    expected_thresholds = [point["threshold"] for point in curves[0]]
    if any([point["threshold"] for point in curve] != expected_thresholds for curve in curves):
        raise ValueError("query threshold curves use different thresholds")

    aggregate: list[dict[str, float | int | None]] = []
    for index, threshold in enumerate(expected_thresholds):
        returned = [curve[index]["returned"] for curve in curves]
        recalls = [
            curve[index]["grade_2_recall"]
            for curve in curves
            if curve[index]["grade_2_recall"] is not None
        ]
        aggregate.append(
            {
                "threshold": threshold,
                "total_returned": sum(returned),
                "mean_returned": sum(returned) / len(returned),
                "macro_grade_2_recall": sum(recalls) / len(recalls) if recalls else None,
                "judged_queries": len(recalls),
            }
        )
    return aggregate


def aggregate_metrics(
    query_reports: Sequence[dict[str, Any]],
    *,
    cutoff: int = METRICS_CUTOFF,
) -> dict[str, float | int | None]:
    """Macro-average available per-query relevance metrics."""
    keys = (
        f"ndcg_at_{cutoff}",
        f"grade_2_recall_at_{cutoff}",
        f"grade_2_mrr_at_{cutoff}",
    )
    aggregate: dict[str, float | int | None] = {"judged_queries": len(query_reports)}
    for key in keys:
        values = [
            report["metrics"][key]
            for report in query_reports
            if report["metrics"][key] is not None
        ]
        aggregate[key] = sum(values) / len(values) if values else None
    return aggregate


def reciprocal_rank_fusion(
    runs: Sequence[dict[str, Any]],
    *,
    top_k: int,
    gold_judgments: GoldJudgments | None = None,
    rank_constant: int = 60,
) -> dict[str, Any] | None:
    """Fuse the reported rankings from two or more embedding runs."""
    usable = [run for run in runs if not run.get("error") and run.get("queries")]
    if len(usable) < 2:
        return None
    query_maps = {
        str(run["label"]): {
            str(query_result["query"]): query_result
            for query_result in cast(Sequence[dict[str, Any]], run["queries"])
        }
        for run in usable
    }
    common_queries = set.intersection(
        *(set(reports) for reports in query_maps.values())
    )
    query_reports: list[dict[str, Any]] = []
    for query in sorted(common_queries):
        scores: dict[int, float] = {}
        payloads: dict[int, dict[str, Any]] = {}
        source_ranks: dict[int, dict[str, int]] = {}
        retrieval_query: str | None = None
        for label, reports in query_maps.items():
            source_report = reports[query]
            if retrieval_query is None:
                retrieval_query = str(source_report.get("retrieval_query") or query)
            for result in cast(Sequence[dict[str, Any]], source_report["results"]):
                position_id = int(result["position_id"])
                rank = int(result["rank"])
                scores[position_id] = scores.get(position_id, 0.0) + 1.0 / (
                    rank_constant + rank
                )
                payloads.setdefault(position_id, result)
                source_ranks.setdefault(position_id, {})[label] = rank
        ordered_ids = sorted(scores, key=lambda item: (-scores[item], item))[:top_k]
        fused_results = [
            {
                **{
                    key: value
                    for key, value in payloads[position_id].items()
                    if key not in {"rank", "score"}
                },
                "rank": rank,
                "rrf_score": scores[position_id],
                "source_ranks": source_ranks[position_id],
            }
            for rank, position_id in enumerate(ordered_ids, start=1)
        ]
        query_report: dict[str, Any] = {
            "query": query,
            "retrieval_query": retrieval_query,
            "results": fused_results,
        }
        judgments = gold_judgments.get(query) if gold_judgments else None
        if judgments:
            query_report["metrics"] = evaluate_ranking(ordered_ids, judgments)
        query_reports.append(query_report)
    report: dict[str, Any] = {
        "method": "reciprocal_rank_fusion",
        "rank_constant": rank_constant,
        "source_depth": top_k,
        "runs": [str(run["label"]) for run in usable],
        "queries": query_reports,
    }
    judged_reports = [query for query in query_reports if "metrics" in query]
    if judged_reports:
        report["metrics"] = aggregate_metrics(judged_reports)
    return report


def _chunks[T](values: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


async def ollama_embed(
    client: httpx.AsyncClient,
    *,
    ollama_url: str,
    model: str,
    inputs: Sequence[str],
    keep_alive: str,
) -> list[list[float]]:
    response = await client.post(
        f"{ollama_url.rstrip('/')}/api/embed",
        json={
            "model": model,
            "input": list(inputs),
            "truncate": True,
            "keep_alive": keep_alive,
        },
    )
    response.raise_for_status()
    payload = response.json()
    embeddings = payload.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(inputs):
        raise RuntimeError(
            f"Ollama returned {len(embeddings) if isinstance(embeddings, list) else 'invalid'} "
            f"embeddings for {len(inputs)} inputs"
        )
    return embeddings


async def _qdrant_position_ids(
    *,
    url: str,
    collection: str,
    limit: int | None,
) -> list[int]:
    client = AsyncQdrantClient(url=url)
    ids: list[int] = []
    offset: Any = None
    try:
        while limit is None or len(ids) < limit:
            page_size = min(256, limit - len(ids)) if limit is not None else 256
            points, next_offset = await client.scroll(
                collection_name=collection,
                limit=page_size,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            ids.extend(int(point.id) for point in points if isinstance(point.id, int))
            if next_offset is None or not points:
                break
            offset = next_offset
    finally:
        await client.close()
    return ids


def _candidate_statement(
    ids: Sequence[int],
) -> Select[tuple[int, str, str, str, str | None, str | None, str | None, str]]:
    return (
        select(
            Position.id,
            Position.title,
            Position.position_type,
            Position.description,
            Position.area,
            Position.full_description,
            Position.institution_name,
            University.name.label("university_name"),
        )
        .outerjoin(University, University.id == Position.university_id)
        .where(Position.id.in_(ids))
        .order_by(Position.id)
    )


async def _fetch_candidates(connection: AsyncConnection, ids: Sequence[int]) -> dict[int, Candidate]:
    candidates: dict[int, Candidate] = {}
    for ids_chunk in _chunks(ids, 1_000):
        result = await connection.execute(_candidate_statement(ids_chunk))
        for row in result.mappings():
            candidate = Candidate(
                position_id=row["id"],
                title=row["title"] or "",
                position_type=row["position_type"] or "other",
                institution=row["university_name"] or row["institution_name"] or "",
                description=row["description"] or "",
                area=row["area"],
                full_description=row["full_description"],
            )
            candidates[candidate.position_id] = candidate
    return candidates


async def load_corpus(
    settings: Settings,
    *,
    indexed_source: Literal["qdrant", "database"],
    limit: int | None,
    extra_ids: Sequence[int],
) -> Corpus:
    """Read the production corpus under a read-only PostgreSQL transaction."""
    if indexed_source == "qdrant":
        indexed_ids = await _qdrant_position_ids(
            url=settings.qdrant.url,
            collection=settings.qdrant.collection,
            limit=limit,
        )
    else:
        engine = create_async_engine(settings.database.url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                statement = select(Position.id).where(Position.indexed_at.is_not(None)).order_by(Position.id)
                if limit is not None:
                    statement = statement.limit(limit)
                indexed_ids = list((await connection.scalars(statement)).all())
        finally:
            await engine.dispose()

    requested_ids = list(dict.fromkeys([*indexed_ids, *extra_ids]))
    engine = create_async_engine(settings.database.url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            candidates_by_id = await _fetch_candidates(connection, requested_ids)
    finally:
        await engine.dispose()
    candidates = [candidates_by_id[position_id] for position_id in requested_ids if position_id in candidates_by_id]
    return Corpus(
        candidates=candidates,
        indexed_ids=len(indexed_ids),
        extra_ids_requested=list(extra_ids),
        missing_ids=[position_id for position_id in requested_ids if position_id not in candidates_by_id],
    )


async def benchmark_variant(
    client: httpx.AsyncClient,
    *,
    spec: ModelSpec,
    candidates: Sequence[Candidate],
    documents: Sequence[str],
    queries: Sequence[str],
    ollama_url: str,
    batch_size: int,
    top_k: int,
    keep_alive: str,
    document_variant: DocumentVariant = "compact",
    score_thresholds: Sequence[float] = DEFAULT_SCORE_THRESHOLDS,
    gold_judgments: GoldJudgments | None = None,
    normalize_queries: bool = True,
) -> dict[str, Any]:
    started = time.perf_counter()
    warmup_started = time.perf_counter()
    _warmup_query, warmup_input = prepare_query_input(
        queries[0],
        spec.profile,
        normalize_retrieval=normalize_queries,
    )
    await ollama_embed(
        client,
        ollama_url=ollama_url,
        model=spec.model,
        inputs=[warmup_input],
        keep_alive=keep_alive,
    )
    warmup_seconds = time.perf_counter() - warmup_started

    query_vectors: list[tuple[float, ...]] = []
    query_latencies: list[float] = []
    retrieval_queries: list[str] = []
    for query in queries:
        retrieval_query, query_input = prepare_query_input(
            query,
            spec.profile,
            normalize_retrieval=normalize_queries,
        )
        query_started = time.perf_counter()
        vectors = await ollama_embed(
            client,
            ollama_url=ollama_url,
            model=spec.model,
            inputs=[query_input],
            keep_alive=keep_alive,
        )
        query_latencies.append(time.perf_counter() - query_started)
        query_vectors.append(normalize_vector(vectors[0]))
        retrieval_queries.append(retrieval_query)

    ranking_depth = max(top_k, METRICS_CUTOFF) if gold_judgments else top_k
    heaps: list[list[tuple[float, int, Candidate]]] = [[] for _ in queries]
    threshold_counts = [[0] * len(score_thresholds) for _ in queries]
    query_judgments = [
        gold_judgments.get(query) if gold_judgments else None for query in queries
    ]
    judged_scores: list[dict[int, float]] = [{} for _ in queries]
    embedding_seconds = 0.0
    scoring_seconds = 0.0
    dimensions: int | None = None
    for start in range(0, len(candidates), batch_size):
        candidate_batch = candidates[start : start + batch_size]
        document_batch = documents[start : start + batch_size]
        embed_started = time.perf_counter()
        vectors = await ollama_embed(
            client,
            ollama_url=ollama_url,
            model=spec.model,
            inputs=[format_document(document, spec.profile) for document in document_batch],
            keep_alive=keep_alive,
        )
        embedding_seconds += time.perf_counter() - embed_started

        score_started = time.perf_counter()
        for candidate, vector in zip(candidate_batch, vectors, strict=True):
            normalized = normalize_vector(vector)
            if dimensions is None:
                dimensions = len(normalized)
            for query_index, query_vector in enumerate(query_vectors):
                score = cosine_normalized(query_vector, normalized)
                update_threshold_counts(
                    threshold_counts[query_index],
                    score=score,
                    thresholds=score_thresholds,
                )
                judgments = query_judgments[query_index]
                if judgments and candidate.position_id in judgments:
                    judged_scores[query_index][candidate.position_id] = score
                update_top_k(
                    heaps[query_index],
                    score=score,
                    candidate=candidate,
                    top_k=ranking_depth,
                )
        scoring_seconds += time.perf_counter() - score_started

    per_query: list[dict[str, Any]] = []
    for query_index, (query, retrieval_query, latency, heap) in enumerate(
        zip(queries, retrieval_queries, query_latencies, heaps, strict=True)
    ):
        ranked = ranked_candidates(heap)
        judgments = query_judgments[query_index]
        query_report: dict[str, Any] = {
            "query": query,
            "retrieval_query": retrieval_query,
            "embedding_latency_ms": round(latency * 1_000, 3),
            "results": [asdict(result) for result in ranked[:top_k]],
            "threshold_curve": build_threshold_curve(
                thresholds=score_thresholds,
                returned_counts=threshold_counts[query_index],
                judgments=judgments,
                judged_scores=judged_scores[query_index],
            ),
        }
        if judgments:
            query_report["metrics"] = evaluate_ranking(
                [result.position_id for result in ranked],
                judgments,
            )
            query_report["judged_scores"] = build_judged_score_report(
                judgments,
                judged_scores[query_index],
            )
        per_query.append(query_report)
    run_report: dict[str, Any] = {
        "model": spec.model,
        "profile": spec.profile,
        "label": f"{spec.label} [{document_variant}]",
        "document_variant": document_variant,
        "dimensions": dimensions,
        "documents": len(candidates),
        "warmup_seconds": round(warmup_seconds, 6),
        "document_embedding_seconds": round(embedding_seconds, 6),
        "document_embeddings_per_second": round(len(candidates) / embedding_seconds, 3)
        if embedding_seconds
        else None,
        "scoring_seconds": round(scoring_seconds, 6),
        "mean_query_embedding_latency_ms": round(
            sum(query_latencies) * 1_000 / len(query_latencies), 3
        ),
        "total_seconds": round(time.perf_counter() - started, 6),
        "queries": per_query,
        "threshold_curve": aggregate_threshold_curves(per_query),
    }
    judged_reports = [report for report in per_query if "metrics" in report]
    if judged_reports:
        run_report["metrics"] = aggregate_metrics(judged_reports)
    return run_report


def render_markdown(report: dict[str, Any]) -> str:
    corpus = report["corpus"]
    lines = [
        "# PHDBOT embedding shadow benchmark",
        "",
        f"Generated: {report['generated_at']}",
        "",
        f"Corpus: **{corpus['documents']}** documents "
        f"({corpus['indexed_ids']} indexed IDs, {len(corpus['extra_ids_requested'])} requested extras).",
        "",
        f"Document construction: {corpus['document_build_seconds']:.3f}s "
        f"({corpus['document_builds_per_second']:.1f} documents/s).",
        "",
    ]
    if interpretation := report.get("evaluation", {}).get("interpretation"):
        lines.extend((f"> **Recall-canary warning:** {interpretation}", ""))
    for variant, build in corpus.get("document_variants", {}).items():
        lines.extend(
            (
                f"Document variant `{variant}`: area={'yes' if build['include_area'] else 'no'}; "
                f"{build['seconds']:.3f}s ({build['documents_per_second']:.1f} documents/s).",
                "",
            )
        )
    if corpus["missing_ids"]:
        lines.extend((f"Missing position IDs: `{corpus['missing_ids']}`", ""))
    for run in report["runs"]:
        lines.extend((f"## {run['label']}", ""))
        if "error" in run:
            lines.extend((f"Error: `{run['error']}`", ""))
            continue
        lines.extend(
            (
                f"Dimensions: {run['dimensions']}; corpus embedding: "
                f"{run['document_embedding_seconds']:.2f}s "
                f"({run['document_embeddings_per_second']:.2f} docs/s); "
                f"mean query embedding latency: {run['mean_query_embedding_latency_ms']:.2f}ms; "
                f"local scoring: {run['scoring_seconds']:.2f}s.",
                "",
            )
        )
        if metrics := run.get("metrics"):
            lines.extend(
                (
                    f"Gold-set macro metrics ({metrics['judged_queries']} queries): "
                    f"nDCG@{METRICS_CUTOFF}={metrics[f'ndcg_at_{METRICS_CUTOFF}']:.4f}; "
                    f"grade-2 recall@{METRICS_CUTOFF}="
                    f"{metrics[f'grade_2_recall_at_{METRICS_CUTOFF}']:.4f}; "
                    f"grade-2 MRR@{METRICS_CUTOFF}="
                    f"{metrics[f'grade_2_mrr_at_{METRICS_CUTOFF}']:.4f}.",
                    "",
                )
            )
        if threshold_curve := run.get("threshold_curve"):
            lines.extend(
                (
                    "Threshold volume/known-positive recall:",
                    "",
                    "| Threshold | Mean returned/query | Total returned | Macro grade-2 recall |",
                    "| ---: | ---: | ---: | ---: |",
                )
            )
            for point in threshold_curve:
                recall = point["macro_grade_2_recall"]
                lines.append(
                    f"| {point['threshold']:.2f} | {point['mean_returned']:.1f} | "
                    f"{point['total_returned']} | "
                    f"{'n/a' if recall is None else f'{recall:.4f}'} |"
                )
            lines.append("")
        for query_report in run["queries"]:
            escaped_query = query_report["query"].replace("|", "\\|")
            lines.extend(
                (
                    f"### {escaped_query}",
                    "",
                    f"Query embedding latency: {query_report['embedding_latency_ms']:.2f}ms.",
                    "",
                    "| Rank | Score | ID | Type | Institution | Title |",
                    "| ---: | ---: | ---: | --- | --- | --- |",
                )
            )
            if metrics := query_report.get("metrics"):
                metric_values = (
                    metrics[f"ndcg_at_{METRICS_CUTOFF}"],
                    metrics[f"grade_2_recall_at_{METRICS_CUTOFF}"],
                    metrics[f"grade_2_mrr_at_{METRICS_CUTOFF}"],
                )
                formatted = ["n/a" if value is None else f"{value:.4f}" for value in metric_values]
                lines.insert(
                    len(lines) - 2,
                    f"Gold: nDCG@{METRICS_CUTOFF}={formatted[0]}; "
                    f"grade-2 recall@{METRICS_CUTOFF}={formatted[1]}; "
                    f"grade-2 MRR@{METRICS_CUTOFF}={formatted[2]}.",
                )
            curve_summary = "; ".join(
                f"{point['threshold']:.2f}: {point['returned']} returned / "
                + (
                    "recall n/a"
                    if point["grade_2_recall"] is None
                    else f"grade-2 recall {point['grade_2_recall']:.3f}"
                )
                for point in query_report["threshold_curve"]
            )
            lines.insert(len(lines) - 2, f"Thresholds: {curve_summary}.")
            if judged_scores := query_report.get("judged_scores"):
                score_summary = "; ".join(
                    f"ID {judgment['position_id']} (g{judgment['grade']})="
                    + (
                        "missing"
                        if judgment["score"] is None
                        else f"{judgment['score']:.4f}"
                    )
                    for judgment in judged_scores
                )
                lines.insert(len(lines) - 2, f"Judged scores: {score_summary}.")
            for result in query_report["results"]:
                values = [
                    str(result["rank"]),
                    f"{result['score']:.4f}",
                    str(result["position_id"]),
                    str(result["position_type"]),
                    str(result["institution"]),
                    str(result["title"]),
                ]
                lines.append("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")
            lines.append("")
    return "\n".join(lines)


async def run_benchmark(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    gold_judgments = load_gold_set(args.gold_set) if args.gold_set else None
    requested_extra_ids = list(args.extra_id)
    if gold_judgments:
        requested_extra_ids = list(
            dict.fromkeys([*requested_extra_ids, *gold_position_ids(gold_judgments)])
        )
    corpus = await load_corpus(
        settings,
        indexed_source=args.indexed_source,
        limit=args.limit,
        extra_ids=requested_extra_ids,
    )
    document_variants: Sequence[DocumentVariant] = args.document_variant or ["compact"]
    score_thresholds: Sequence[float] = args.threshold or DEFAULT_SCORE_THRESHOLDS
    documents_by_variant: dict[DocumentVariant, list[str]] = {}
    document_build_reports: dict[str, dict[str, float | bool]] = {}
    document_build_seconds = 0.0
    for document_variant in document_variants:
        build_started = time.perf_counter()
        documents = [
            build_candidate_document(
                candidate,
                description_chars=args.description_chars,
                include_full_description=args.include_full_description,
                include_area=document_variant == "compact-area",
            )
            for candidate in corpus.candidates
        ]
        variant_seconds = time.perf_counter() - build_started
        document_build_seconds += variant_seconds
        documents_by_variant[document_variant] = documents
        document_build_reports[document_variant] = {
            "include_area": document_variant == "compact-area",
            "seconds": variant_seconds,
            "documents_per_second": len(documents) / variant_seconds
            if variant_seconds
            else 0.0,
        }

    runs: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout)) as client:
        for spec in args.model:
            for document_variant in document_variants:
                try:
                    runs.append(
                        await benchmark_variant(
                            client,
                            spec=spec,
                            candidates=corpus.candidates,
                            documents=documents_by_variant[document_variant],
                            queries=args.query,
                            ollama_url=args.ollama_url,
                            batch_size=args.batch,
                            top_k=args.top_k,
                            keep_alive=args.keep_alive,
                            document_variant=document_variant,
                            score_thresholds=score_thresholds,
                            gold_judgments=gold_judgments,
                            normalize_queries=args.normalize_queries,
                        )
                    )
                except (httpx.HTTPError, RuntimeError, ValueError) as error:
                    runs.append(
                        {
                            "model": spec.model,
                            "profile": spec.profile,
                            "label": f"{spec.label} [{document_variant}]",
                            "document_variant": document_variant,
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )

    fusion = reciprocal_rank_fusion(
        runs,
        top_k=args.top_k,
        gold_judgments=gold_judgments,
    )
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "corpus": {
            "indexed_source": args.indexed_source,
            "indexed_ids": corpus.indexed_ids,
            "documents": len(corpus.candidates),
            "extra_ids_requested": corpus.extra_ids_requested,
            "missing_ids": corpus.missing_ids,
            "limit": args.limit,
            "description_chars": args.description_chars,
            "include_full_description": args.include_full_description,
            "document_variants": document_build_reports,
            "document_build_seconds": document_build_seconds,
            "document_builds_per_second": (
                len(corpus.candidates) * len(document_variants) / document_build_seconds
            )
            if document_build_seconds
            else None,
        },
        "top_k": args.top_k,
        "queries": list(args.query),
        "query_normalization": (
            "production_acronym_expansion"
            if args.normalize_queries
            else "disabled"
        ),
        "evaluation": {
            "gold_set": str(args.gold_set) if args.gold_set else None,
            "schema_version": GOLD_SCHEMA_VERSION if gold_judgments else None,
            "cutoff": METRICS_CUTOFF if gold_judgments else None,
            "score_thresholds": list(score_thresholds),
            "gold_set_queries": len(gold_judgments) if gold_judgments else 0,
            "requested_judged_queries": sum(
                query in gold_judgments for query in args.query
            )
            if gold_judgments
            else 0,
            "interpretation": (
                "Incomplete positive-only recall canary: unjudged results are unknown, "
                "not negative. Threshold return counts are exact for this corpus, but "
                "precision requires a separate pooled manual audit."
                if gold_judgments
                else None
            ),
        },
        "runs": runs,
        "fusion": fusion,
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _score_threshold(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not -1 <= parsed <= 1:
        raise argparse.ArgumentTypeError("threshold must be a finite cosine score from -1 to 1")
    return parsed


def build_parser(settings: Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only local shadow benchmark of embedding strategies against current PHDBOT candidates."
    )
    parser.add_argument(
        "--model",
        action="append",
        type=parse_model_spec,
        default=None,
        metavar="MODEL[::PROFILE]",
        help="repeatable Ollama model/profile; profiles: raw, nomic, qwen",
    )
    parser.add_argument("--limit", type=_positive_int, default=None, help="cap current indexed candidates")
    parser.add_argument("--batch", type=_positive_int, default=32, help="Ollama document embedding batch size")
    parser.add_argument("--top-k", type=_positive_int, default=10)
    parser.add_argument("--extra-id", type=_positive_int, action="append", default=[], help="include a DB position ID")
    parser.add_argument(
        "--query",
        action="append",
        default=None,
        help="repeatable query; replaces the built-in design/XR multilingual set",
    )
    parser.add_argument(
        "--raw-queries",
        action="store_false",
        dest="normalize_queries",
        help="disable PHDBOT's production acronym normalization for a diagnostic A/B",
    )
    parser.add_argument("--description-chars", type=int, default=800, help="0 keeps the selected description intact")
    parser.add_argument(
        "--include-full-description",
        action="store_true",
        help="use full_description when present (still subject to --description-chars)",
    )
    parser.add_argument(
        "--document-variant",
        action="append",
        choices=("compact", "compact-area"),
        default=None,
        help=(
            "repeatable document contract; pass compact and compact-area for an "
            "A/B that does not alter production indexing"
        ),
    )
    parser.add_argument(
        "--threshold",
        action="append",
        type=_score_threshold,
        default=None,
        help="repeatable cosine threshold (default: 0.45, 0.50, ..., 0.70)",
    )
    parser.add_argument("--indexed-source", choices=("qdrant", "database"), default="qdrant")
    parser.add_argument("--ollama-url", default=settings.embedding.api_base or "http://localhost:11434")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds per Ollama request")
    parser.add_argument("--keep-alive", default="10m")
    gold_group = parser.add_mutually_exclusive_group()
    gold_group.add_argument(
        "--gold-set",
        type=Path,
        default=DEFAULT_GOLD_SET,
        help="graded relevance JSON (default: the versioned PHDBOT gold set)",
    )
    gold_group.add_argument(
        "--no-gold-set",
        action="store_const",
        const=None,
        dest="gold_set",
        help="disable judged relevance metrics and automatic gold-ID inclusion",
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path, help="write report here instead of stdout")
    return parser


def main() -> None:
    load_dotenv()
    settings = Settings()
    parser = build_parser(settings)
    args = parser.parse_args()
    if args.description_chars < 0:
        parser.error("--description-chars must be zero or greater")
    # argparse append does not replace a non-None default, so install defaults
    # only after parsing and let any explicit --model/--query fully replace them.
    if args.model is None:
        current_model = settings.embedding.model.removeprefix("ollama/")
        args.model = [ModelSpec(current_model, "raw")]
        if "nomic" in current_model.lower():
            args.model.append(ModelSpec(current_model, "nomic"))
        args.model.append(ModelSpec("qwen3-embedding:0.6b", "qwen"))
    if args.query is None:
        args.query = list(DEFAULT_QUERIES)
    args.document_variant = list(dict.fromkeys(args.document_variant or ["compact"]))
    args.threshold = sorted(set(args.threshold or DEFAULT_SCORE_THRESHOLDS))

    report = asyncio.run(run_benchmark(args, settings))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else render_markdown(report)
    if args.output:
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    else:
        sys.stdout.write(f"{rendered}\n")


if __name__ == "__main__":
    main()
