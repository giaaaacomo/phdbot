"""Search-engine discovery config."""

from __future__ import annotations

from pydantic import BaseModel


class SearchConfig(BaseModel):
    provider: str = "ddg"  # ddg | brave | none
    api_key: str | None = None  # solo brave
    max_results: int = 10
