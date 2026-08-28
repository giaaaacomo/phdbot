"""Thin litellm wrapper for completion + embedding. No provider branching."""

from __future__ import annotations

from typing import Literal

import litellm

from phd_searcher.config.llm import EmbeddingConfig, LLMConfig
from phd_searcher.engine.search_documents import (
    CANDIDATE_SEARCH_DOCUMENT_CONTRACT,
    INSTITUTION_SEARCH_DOCUMENT_CONTRACT,
)

QWEN_RETRIEVAL_INSTRUCTION = (
    "Given a search query about academic opportunities, retrieve relevant "
    "academic job and study opportunity descriptions"
)
EmbeddingProfile = Literal["nomic", "qwen", "raw"]
EMBEDDING_INPUT_CONTRACT_VERSION: dict[EmbeddingProfile, str] = {
    "nomic": "nomic-v1",
    "qwen": "qwen-v1",
    "raw": "raw-v1",
}


def embedding_profile_for_model(model: str) -> EmbeddingProfile:
    """Resolve known retrieval contracts and safely default to raw inputs."""
    family = model.casefold().rsplit("/", 1)[-1].split(":", 1)[0]
    if family == "nomic-embed-text" or family.startswith("nomic-embed-"):
        return "nomic"
    if family == "qwen3-embedding" or family.startswith("qwen3-embedding-"):
        return "qwen"
    return "raw"


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

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Embed retrieval queries using the selected model's input contract."""
        return await self.embed([self._query_input(text) for text in texts])

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed retrieval documents using the selected model's input contract."""
        return await self.embed([self._document_input(text) for text in texts])

    def search_index_contract(self, *, institutions: bool = False) -> str:
        """Stable guard against mixing incompatible vectors in one collection."""
        document_contract = (
            INSTITUTION_SEARCH_DOCUMENT_CONTRACT
            if institutions
            else CANDIDATE_SEARCH_DOCUMENT_CONTRACT
        )
        configured_model = str(getattr(self._embedding, "model", "")).strip().casefold()
        profile = embedding_profile_for_model(configured_model)
        input_contract = EMBEDDING_INPUT_CONTRACT_VERSION[profile]
        return f"{document_contract}|{input_contract}|{configured_model}"

    def _query_input(self, text: str) -> str:
        embedding = getattr(self, "_embedding", None)
        profile = embedding_profile_for_model(str(getattr(embedding, "model", "")))
        if profile == "nomic":
            return f"search_query: {text}"
        if profile == "qwen":
            return f"Instruct: {QWEN_RETRIEVAL_INSTRUCTION}\nQuery: {text}"
        return text

    def _document_input(self, text: str) -> str:
        embedding = getattr(self, "_embedding", None)
        profile = embedding_profile_for_model(str(getattr(embedding, "model", "")))
        if profile == "nomic":
            return f"search_document: {text}"
        return text
