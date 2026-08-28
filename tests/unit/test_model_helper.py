from unittest.mock import AsyncMock

import pytest

from phd_searcher.config.llm import EmbeddingConfig, LLMConfig
from phd_searcher.engine.model_helper import ModelHelper, embedding_profile_for_model


def _helper(model: str) -> ModelHelper:
    return ModelHelper(
        llm=LLMConfig(model="test/llm"),
        embedding=EmbeddingConfig(model=model),
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("ollama/nomic-embed-text", "nomic"),
        ("ollama/qwen3-embedding:0.6b", "qwen"),
        ("ollama/qwen3.6:35b-a3b", "raw"),
        ("custom/nomic-reranker", "raw"),
        ("azure/text-embedding-3-large", "raw"),
        ("", "raw"),
    ],
)
def test_embedding_profile_detection_has_safe_raw_fallback(
    model: str,
    expected: str,
) -> None:
    assert embedding_profile_for_model(model) == expected


def test_search_index_contract_pins_document_profile_and_model() -> None:
    helper = _helper("ollama/NOMIC-embed-text")

    assert helper.search_index_contract() == (
        "candidate-compact-v2|nomic-v1|ollama/nomic-embed-text"
    )
    assert helper.search_index_contract(institutions=True) == (
        "institution-compact-v1|nomic-v1|ollama/nomic-embed-text"
    )


@pytest.mark.parametrize(
    ("model", "query", "document"),
    [
        (
            "ollama/nomic-embed-text",
            "search_query: virtual reality",
            "search_document: Candidate text",
        ),
        (
            "ollama/qwen3-embedding:0.6b",
            (
                "Instruct: Given a search query about academic opportunities, "
                "retrieve relevant academic job and study opportunity descriptions\n"
                "Query: virtual reality"
            ),
            "Candidate text",
        ),
        ("test/generic-embedding", "virtual reality", "Candidate text"),
    ],
)
async def test_embedding_methods_apply_model_contract(
    model: str,
    query: str,
    document: str,
) -> None:
    helper = _helper(model)
    raw_embed = AsyncMock(return_value=[[1.0]])
    helper.embed = raw_embed

    assert await helper.embed_queries(["virtual reality"]) == [[1.0]]
    raw_embed.assert_awaited_once_with([query])

    raw_embed.reset_mock()
    assert await helper.embed_documents(["Candidate text"]) == [[1.0]]
    raw_embed.assert_awaited_once_with([document])


async def test_embedding_contract_methods_remain_compatible_with_embed_only_fake() -> None:
    class FakeModelHelper(ModelHelper):
        def __init__(self) -> None:
            pass

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[float(len(text))] for text in texts]

    fake = FakeModelHelper()

    assert await fake.embed_queries(["XR"]) == [[len("XR")]]
    assert await fake.embed_documents(["VR"]) == [[len("VR")]]
