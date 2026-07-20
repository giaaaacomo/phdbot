from injector import Module, provider, singleton
from qdrant_client import AsyncQdrantClient

from phd_searcher.config.qdrant import QdrantConfig


class QdrantModule(Module):
    @singleton
    @provider
    def provide_qdrant(self, config: QdrantConfig) -> AsyncQdrantClient:
        return AsyncQdrantClient(url=config.url)
