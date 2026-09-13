from unittest.mock import AsyncMock

import httpx
import pytest

from phd_searcher.config.llm import EmbeddingConfig, LLMConfig
from phd_searcher.engine.model_helper import ModelHelper
from phd_searcher.pipeline.discovery_selection import DiscoverySelectionExhaustedError, select_listings
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.retry import RetryInterruptedError

URL = "https://institute.example/jobs"
PROMPT = "Select recruitment listing pages with select_listing_pages."


def message(args, *, name="select_listing_pages", call_id=None):
    call = {"function": {"name": name, "arguments": args}}
    if call_id:
        call["id"] = call_id
    return {"role": "assistant", "tool_calls": [call]}


@pytest.mark.parametrize("bad", ["{broken", {"urls": [123]}, {"urls": ["https://invented.example/jobs"]}, {"other": []}])
@pytest.mark.parametrize("call_id", [None, "call-1"])
async def test_invalid_selection_gets_tool_feedback_then_corrects(bad, call_id):
    model = AsyncMock()
    model.complete_with_tools.side_effect = [message(bad, call_id=call_id), message({"urls": [URL, URL]})]
    assert await select_listings(model, PROMPT, {URL}, Progress()) == [URL]
    history = model.complete_with_tools.call_args_list[1].args[0]
    feedback = next(m for m in history if m["role"] == "tool")
    assert "Validation failed" in feedback["content"]
    assert feedback.get("tool_call_id", feedback.get("tool_name")) == (call_id or "select_listing_pages")


async def test_valid_empty_is_accepted_but_text_empty_is_not():
    model = AsyncMock()
    model.complete_with_tools.return_value = message({"urls": []})
    assert await select_listings(model, PROMPT, {URL}, Progress()) == []
    model.complete_with_tools.return_value = {"role": "assistant", "content": "[]"}
    with pytest.raises(DiscoverySelectionExhaustedError, match="3 tool attempts"):
        await select_listings(model, PROMPT, {URL}, Progress())
    assert model.complete_with_tools.await_count == 4


async def test_wrong_tool_and_multiple_calls_are_not_accepted():
    model = AsyncMock()
    wrong = message({"urls": [URL]}, name="other_tool")
    multiple = message({"urls": [URL]})
    multiple["tool_calls"] *= 2
    model.complete_with_tools.side_effect = [wrong, multiple, message({"urls": [URL]})]
    assert await select_listings(model, PROMPT, {URL}, Progress()) == [URL]


async def test_stop_does_not_generate():
    model = AsyncMock()
    progress = Progress()
    progress.should_stop = True
    with pytest.raises(RetryInterruptedError):
        await select_listings(model, PROMPT, {URL}, progress)
    model.complete_with_tools.assert_not_called()


async def test_native_bad_request_feedback_is_bounded_and_429_propagates():
    model = AsyncMock()
    for code in (400, 429):
        response = httpx.Response(code, request=httpx.Request("POST", "http://ollama.test/api/chat"))
        model.complete_with_tools.side_effect = httpx.HTTPStatusError("bad", request=response.request, response=response)
        expected = DiscoverySelectionExhaustedError if code == 400 else httpx.HTTPStatusError
        with pytest.raises(expected):
            await select_listings(model, PROMPT, {URL}, Progress())


async def test_native_transport_preserves_tools_and_bounds_generation(monkeypatch):
    original = httpx.AsyncClient
    captured = []

    def respond(request):
        import json
        captured.append(json.loads(request.content))
        assert str(request.url) == "http://ollama.test/api/chat"
        return httpx.Response(200, json={"message": message({"urls": [URL]})})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(respond)))
    model = ModelHelper(LLMConfig(model="ollama/test", api_base="http://ollama.test/v1"), EmbeddingConfig(model="test"))
    assert await select_listings(model, PROMPT, {URL}, Progress()) == [URL]
    payload = captured[0]
    assert payload["tools"][0]["function"]["name"] == "select_listing_pages"
    assert payload["options"]["num_predict"] == 2048
    assert "format" not in payload
