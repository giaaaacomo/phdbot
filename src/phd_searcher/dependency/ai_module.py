from injector import Module, provider, singleton

from phd_searcher.config.llm import EmbeddingConfig, LLMConfig
from phd_searcher.engine.model_helper import ModelHelper


class AIModule(Module):
    @singleton
    @provider
    def provide_model_helper(self, llm: LLMConfig, embedding: EmbeddingConfig) -> ModelHelper:
        return ModelHelper(llm=llm, embedding=embedding)
