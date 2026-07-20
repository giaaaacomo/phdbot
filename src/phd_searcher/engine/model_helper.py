"""Thin litellm wrapper for completion + embedding. No provider branching."""

from __future__ import annotations

import litellm

from phd_searcher.config.llm import EmbeddingConfig, LLMConfig


class ModelHelper:
    def __init__(self, llm: LLMConfig, embedding: EmbeddingConfig) -> None:
        self._llm = llm
        self._embedding = embedding

    async def complete(self, messages: list[dict[str, str]]) -> str:
        resp = await litellm.acompletion(
            model=self._llm.model,
            messages=messages,
            api_base=self._llm.api_base,
            api_key=self._llm.api_key,
            temperature=self._llm.temperature,
        )
        return resp.choices[0].message.content or ""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await litellm.aembedding(
            model=self._embedding.model,
            input=texts,
            api_base=self._embedding.api_base,
            api_key=self._embedding.api_key,
        )
        return [item["embedding"] for item in resp.data]
