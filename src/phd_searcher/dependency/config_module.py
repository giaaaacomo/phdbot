from injector import Module, provider, singleton

from phd_searcher.config import Settings
from phd_searcher.config.database import DatabaseConfig
from phd_searcher.config.export import ExportConfig
from phd_searcher.config.llm import EmbeddingConfig, LLMConfig
from phd_searcher.config.qdrant import QdrantConfig
from phd_searcher.config.search import SearchConfig


class ConfigModule(Module):
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    @singleton
    @provider
    def provide_settings(self) -> Settings:
        if self._settings is None:
            self._settings = Settings()  # env-driven (pydantic-settings reads env)
        return self._settings

    @singleton
    @provider
    def provide_llm_config(self, settings: Settings) -> LLMConfig:
        return settings.llm

    @singleton
    @provider
    def provide_embedding_config(self, settings: Settings) -> EmbeddingConfig:
        return settings.embedding

    @singleton
    @provider
    def provide_db_config(self, settings: Settings) -> DatabaseConfig:
        return settings.database

    @singleton
    @provider
    def provide_qdrant_config(self, settings: Settings) -> QdrantConfig:
        return settings.qdrant

    @singleton
    @provider
    def provide_search_config(self, settings: Settings) -> SearchConfig:
        return settings.search

    @singleton
    @provider
    def provide_export_config(self, settings: Settings) -> ExportConfig:
        return settings.export
