"""Unit-test fixtures: container con ModelHelper fake e Qdrant in-memory (no network)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from injector import Injector, Module, provider, singleton
from qdrant_client import AsyncQdrantClient

from phd_searcher.config import Settings
from phd_searcher.config.database import DatabaseConfig
from phd_searcher.config.llm import EmbeddingConfig, LLMConfig
from phd_searcher.config.qdrant import QdrantConfig
from phd_searcher.config.search import SearchConfig
from phd_searcher.dependency.config_module import ConfigModule
from phd_searcher.dependency.service_module import ServiceModule
from phd_searcher.engine.model_helper import ModelHelper
from phd_searcher.main import create_app

VECTOR_DIM = 4


class FakeModelHelper(ModelHelper):
    def __init__(self, reply: str = "canned answer") -> None:
        self._reply = reply
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self._reply

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (VECTOR_DIM - 1) for _ in texts]


@pytest.fixture
def fake_model() -> FakeModelHelper:
    return FakeModelHelper()


@pytest.fixture
def qdrant() -> AsyncQdrantClient:
    return AsyncQdrantClient(location=":memory:")


@pytest.fixture
def container(fake_model: FakeModelHelper, qdrant: AsyncQdrantClient) -> Injector:
    settings = Settings(
        log_level="INFO",
        llm=LLMConfig(model="test/model"),
        embedding=EmbeddingConfig(model="test/embedding"),
        database=DatabaseConfig(url="postgresql+asyncpg://u:p@localhost:5432/test"),
        qdrant=QdrantConfig(collection="positions"),
        search=SearchConfig(provider="none"),
    )

    class FakeAIModule(Module):
        @singleton
        @provider
        def provide_model_helper(self) -> ModelHelper:
            return fake_model

    class FakeQdrantModule(Module):
        @singleton
        @provider
        def provide_qdrant(self) -> AsyncQdrantClient:
            return qdrant

    return Injector([ConfigModule(settings), FakeAIModule(), FakeQdrantModule(), ServiceModule()])


@pytest.fixture
def client(container: Injector) -> Iterator[TestClient]:
    with TestClient(create_app(container, title="test", version="0.0.0")) as c:
        yield c
