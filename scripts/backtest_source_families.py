"""Read-only, leakage-aware backtest for URL source-family priors."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from phd_searcher.pipeline.source_family import (
    FamilyCounts,
    FamilyDirection,
    family_direction,
    source_family_keys,
)

_SPACE_RE = re.compile(r"\s+")
_STRICT_VERSIONS = frozenset({"evidence-v19", "evidence-v20", "evidence-v21", "evidence-v22", "evidence-v23"})


@dataclass(frozen=True, slots=True)
class LabelledRow:
    position_id: int
    listing_page_id: int
    position_url: str
    listing_url: str
    title: str
    institution: str
    document: str
    deadline_raw: str
    published_raw: str
    label: bool
    labelled_at: datetime
    version: str
    strict: bool

    @property
    def fingerprint(self) -> str:
        """Group duplicate evidence without including any predicted label."""
        values = (
            self.institution,
            self.title,
            self.document,
            self.deadline_raw,
            self.published_raw,
        )
        canonical = "\x1f".join(_SPACE_RE.sub(" ", value).strip().casefold() for value in values)
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class UnresolvedRow:
    position_id: int
    listing_page_id: int
    position_url: str
    listing_url: str


@dataclass(frozen=True, slots=True)
class Evaluation:
    cohort: str
    scope: str
    mode: str
    minimum_samples: int
    labelled: int
    opportunities: int
    non_opportunities: int
    covered: int
    correct: int
    predicted_opportunity: int
    predicted_non_opportunity: int
    false_positive: int
    false_negative: int

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result |= {
            "coverage": self.covered / self.labelled if self.labelled else 0.0,
            "selective_accuracy": self.correct / self.covered if self.covered else None,
            "opportunity_precision": (
                (self.predicted_opportunity - self.false_positive) / self.predicted_opportunity
                if self.predicted_opportunity
                else None
            ),
            "non_opportunity_precision": (
                (self.predicted_non_opportunity - self.false_negative) / self.predicted_non_opportunity
                if self.predicted_non_opportunity
                else None
            ),
            "false_negative_rate": self.false_negative / self.opportunities if self.opportunities else None,
            "false_positive_rate": self.false_positive / self.non_opportunities if self.non_opportunities else None,
        }
        return result


_LABEL_SQL = text(
    """
    WITH ranked AS (
        SELECT ra.*,
               row_number() OVER (
                   PARTITION BY ra.position_id
                   ORDER BY ra.created_at DESC, ra.id DESC
               ) AS rn
        FROM review_attempts ra
        WHERE ra.stage = 'review2'
    )
    SELECT p.id AS position_id,
           p.listing_page_id,
           p.url AS position_url,
           lp.url AS listing_url,
           p.title,
           COALESCE(u.wikidata_id, p.institution_name, '') AS institution,
           COALESCE(p.full_description, p.description, '') AS document,
           COALESCE(p.deadline_raw, '') AS deadline_raw,
           COALESCE(p.published_raw, '') AS published_raw,
           r.details->>'actual_vacancy' AS actual_vacancy,
           r.created_at AS labelled_at,
           r.version
    FROM ranked r
    JOIN positions p ON p.id = r.position_id
    JOIN listing_pages lp ON lp.id = p.listing_page_id
    LEFT JOIN universities u ON u.id = p.university_id
    WHERE r.rn = 1
      AND r.accepted_status IN ('eligible', 'rejected')
      AND jsonb_typeof(r.evidence) = 'array'
      AND jsonb_array_length(r.evidence) > 0
      AND COALESCE(r.details->>'evidence_sufficient', 'false') = 'true'
      AND COALESCE(r.details->>'tool_error', 'false') = 'false'
      AND r.details->>'reused_from_position_id' IS NULL
      AND r.details->>'actual_vacancy' IN ('yes', 'no')
      AND r.confidence >= CASE r.accepted_status WHEN 'eligible' THEN 0.90 ELSE 0.97 END
    ORDER BY r.created_at, p.id
    """
)

_UNRESOLVED_SQL = text(
    """
    SELECT p.id AS position_id,
           p.listing_page_id,
           p.url AS position_url,
           lp.url AS listing_url
    FROM positions p
    JOIN listing_pages lp ON lp.id = p.listing_page_id
    WHERE p.is_active IS TRUE
      AND p.screening_manual IS FALSE
      AND p.screening_status IN ('pending', 'review')
    ORDER BY p.id
    """
)


def _family_keys(row: LabelledRow | UnresolvedRow, scope: Literal["listing", "host"]) -> tuple[str, ...]:
    return source_family_keys(
        row.position_url,
        listing_url=row.listing_url,
        listing_page_id=row.listing_page_id if scope == "listing" else None,
    )


def _aggregate(
    rows: list[LabelledRow],
    *,
    scope: Literal["listing", "host"],
) -> tuple[dict[str, FamilyCounts], dict[tuple[str, str], FamilyCounts]]:
    counts: dict[str, FamilyCounts] = {}
    fingerprint_counts: dict[tuple[str, str], FamilyCounts] = {}
    for row in rows:
        for key in _family_keys(row, scope):
            counts[key] = counts.get(key, FamilyCounts()).adding(row.label)
            fingerprint_key = (row.fingerprint, key)
            fingerprint_counts[fingerprint_key] = fingerprint_counts.get(fingerprint_key, FamilyCounts()).adding(
                row.label
            )
    return counts, fingerprint_counts


def _prediction(
    row: LabelledRow | UnresolvedRow,
    counts: dict[str, FamilyCounts],
    *,
    scope: Literal["listing", "host"],
    minimum_samples: int,
    excluded: dict[str, FamilyCounts] | None = None,
) -> FamilyDirection:
    for key in _family_keys(row, scope):
        observed = counts.get(key, FamilyCounts())
        if excluded and key in excluded:
            observed = observed.subtracting(excluded[key])
        direction = family_direction(observed, minimum_samples=minimum_samples)
        if direction != FamilyDirection.INSUFFICIENT:
            return direction
    return FamilyDirection.INSUFFICIENT


def _score(
    rows: list[LabelledRow],
    *,
    cohort: str,
    scope: Literal["listing", "host"],
    mode: str,
    minimum_samples: int,
    train: list[LabelledRow] | None = None,
) -> Evaluation:
    training = train if train is not None else rows
    counts, fingerprint_counts = _aggregate(training, scope=scope)
    covered = correct = predicted_opportunity = predicted_non_opportunity = false_positive = false_negative = 0
    training_ids = {row.position_id for row in training}

    for row in rows:
        excluded: dict[str, FamilyCounts] = {}
        if row.position_id in training_ids:
            excluded = {
                key: fingerprint_counts[(row.fingerprint, key)]
                for key in _family_keys(row, scope)
                if (row.fingerprint, key) in fingerprint_counts
            }
        direction = _prediction(
            row,
            counts,
            scope=scope,
            minimum_samples=minimum_samples,
            excluded=excluded,
        )
        if direction not in {
            FamilyDirection.SUPPORTS_OPPORTUNITY,
            FamilyDirection.SUPPORTS_NON_OPPORTUNITY,
        }:
            continue
        covered += 1
        predicted = direction == FamilyDirection.SUPPORTS_OPPORTUNITY
        predicted_opportunity += int(predicted)
        predicted_non_opportunity += int(not predicted)
        correct += int(predicted == row.label)
        false_positive += int(predicted and not row.label)
        false_negative += int(not predicted and row.label)

    return Evaluation(
        cohort=cohort,
        scope=scope,
        mode=mode,
        minimum_samples=minimum_samples,
        labelled=len(rows),
        opportunities=sum(row.label for row in rows),
        non_opportunities=sum(not row.label for row in rows),
        covered=covered,
        correct=correct,
        predicted_opportunity=predicted_opportunity,
        predicted_non_opportunity=predicted_non_opportunity,
        false_positive=false_positive,
        false_negative=false_negative,
    )


def evaluate(rows: list[LabelledRow], cohort: str) -> list[Evaluation]:
    """Evaluate LOO plus a duplicate-safe chronological holdout."""
    results: list[Evaluation] = []
    if not rows:
        return results
    ordered = sorted(rows, key=lambda row: (row.labelled_at, row.position_id))
    split = max(1, int(len(ordered) * 0.70))
    temporal_train = ordered[:split]
    temporal_test = ordered[split:]
    test_fingerprints = {row.fingerprint for row in temporal_test}
    temporal_train = [row for row in temporal_train if row.fingerprint not in test_fingerprints]
    for scope in ("listing", "host"):
        for minimum_samples in (2, 3, 5, 10, 20):
            results.append(
                _score(
                    rows,
                    cohort=cohort,
                    scope=scope,
                    mode="group_leave_one_out",
                    minimum_samples=minimum_samples,
                )
            )
            if temporal_test:
                results.append(
                    _score(
                        temporal_test,
                        cohort=cohort,
                        scope=scope,
                        mode="temporal_holdout",
                        minimum_samples=minimum_samples,
                        train=temporal_train,
                    )
                )
    return results


async def _load(engine: AsyncEngine) -> tuple[list[LabelledRow], list[UnresolvedRow]]:
    async with engine.connect() as connection:
        labelled_result = await connection.execute(_LABEL_SQL)
        unresolved_result = await connection.execute(_UNRESOLVED_SQL)
        labelled = [
            LabelledRow(
                position_id=row.position_id,
                listing_page_id=row.listing_page_id,
                position_url=row.position_url,
                listing_url=row.listing_url,
                title=row.title,
                institution=row.institution,
                document=row.document,
                deadline_raw=row.deadline_raw,
                published_raw=row.published_raw,
                label=row.actual_vacancy == "yes",
                labelled_at=row.labelled_at,
                version=row.version,
                strict=row.version in _STRICT_VERSIONS,
            )
            for row in labelled_result
        ]
        unresolved = [
            UnresolvedRow(
                position_id=row.position_id,
                listing_page_id=row.listing_page_id,
                position_url=row.position_url,
                listing_url=row.listing_url,
            )
            for row in unresolved_result
        ]
    return labelled, unresolved


def _backlog_projection(
    labels: list[LabelledRow],
    unresolved: list[UnresolvedRow],
    *,
    scope: Literal["listing", "host"],
    minimum_samples: int,
) -> dict[str, int]:
    counts, _fingerprints = _aggregate(labels, scope=scope)
    directions = Counter(
        _prediction(row, counts, scope=scope, minimum_samples=minimum_samples).value for row in unresolved
    )
    return dict(sorted(directions.items()))


async def backtest(database_url: str) -> dict[str, Any]:
    engine = create_async_engine(database_url)
    try:
        all_labels, unresolved = await _load(engine)
    finally:
        await engine.dispose()

    cohorts = {
        "strict_recent": [row for row in all_labels if row.strict],
        "extended_silver": all_labels,
    }
    evaluations = [result.as_dict() for name, rows in cohorts.items() for result in evaluate(rows, name)]
    projections = {
        name: {
            scope: {
                str(minimum): _backlog_projection(
                    rows,
                    unresolved,
                    scope=scope,
                    minimum_samples=minimum,
                )
                for minimum in (5, 10, 20)
            }
            for scope in ("listing", "host")
        }
        for name, rows in cohorts.items()
    }
    return {
        "warning": (
            "Agreement with evidence-grounded historical labels is not a gold-set accuracy estimate. "
            "No family prior may infer current open/closed status or create a hard rejection."
        ),
        "labels": {
            name: {
                "rows": len(rows),
                "independent_fingerprints": len({row.fingerprint for row in rows}),
                "opportunities": sum(row.label for row in rows),
                "non_opportunities": sum(not row.label for row in rows),
            }
            for name, rows in cohorts.items()
        },
        "unresolved_rows": len(unresolved),
        "evaluations": evaluations,
        "backlog_projection": projections,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "PHD_SEARCHER__DATABASE__URL",
            "postgresql+asyncpg://app:app@localhost:5433/app",
        ),
    )
    parser.add_argument("--output", choices=("pretty", "json"), default="pretty")
    args = parser.parse_args()
    report = asyncio.run(backtest(args.database_url))
    if args.output == "json":
        print(json.dumps(report, indent=2, default=str))
        return

    print(report["warning"])
    print(json.dumps(report["labels"], indent=2))
    print(f"unresolved rows: {report['unresolved_rows']}")
    print("\nGroup-LOO configurations with non-zero coverage:")
    for result in report["evaluations"]:
        if result["mode"] == "group_leave_one_out" and result["covered"]:
            print(json.dumps(result, sort_keys=True))
    print("\nProjected routing counts (shadow only):")
    print(json.dumps(report["backlog_projection"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
