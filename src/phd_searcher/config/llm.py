"""LLM + embedding config. Provider-neutral via litellm model strings."""

from __future__ import annotations

from pydantic import BaseModel


class LLMConfig(BaseModel):
    model: str  # litellm model string, e.g. "azure/gpt-4o"
    api_base: str | None = None
    api_key: str | None = None
    temperature: float = 0.0


class EmbeddingConfig(BaseModel):
    model: str  # e.g. "azure/text-embedding-3-large"
    api_base: str | None = None
    api_key: str | None = None
