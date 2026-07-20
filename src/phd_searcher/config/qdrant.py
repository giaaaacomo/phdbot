"""Qdrant connection config."""

from __future__ import annotations

from pydantic import BaseModel


class QdrantConfig(BaseModel):
    url: str = "http://localhost:6333"
    collection: str = "positions"
