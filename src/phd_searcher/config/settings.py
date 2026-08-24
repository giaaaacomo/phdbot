"""Process-wide configuration, read from env with prefix PHD_SEARCHER__."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from phd_searcher.config.database import DatabaseConfig
from phd_searcher.config.export import ExportConfig
from phd_searcher.config.llm import EmbeddingConfig, LLMConfig
from phd_searcher.config.qdrant import QdrantConfig
from phd_searcher.config.search import SearchConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PHD_SEARCHER__",
        env_nested_delimiter="__",
        extra="ignore",
    )

    log_level: str = "INFO"
    llm: LLMConfig
    embedding: EmbeddingConfig
    database: DatabaseConfig
    qdrant: QdrantConfig = QdrantConfig()
    search: SearchConfig = SearchConfig()
    export: ExportConfig = ExportConfig()
