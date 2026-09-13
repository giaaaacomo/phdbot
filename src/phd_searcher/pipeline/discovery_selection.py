"""Bounded, validated listing selection with actionable tool feedback."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from phd_searcher.engine.model_helper import ModelHelper
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.retry import RetryInterruptedError

_LOGGER = logging.getLogger(__name__)


class DiscoverySelectionExhaustedError(RuntimeError):
    """The validation budget is spent; do not multiply it with transport retries."""


async def select_listings(
    model: ModelHelper, prompt: str, allowed: set[str] | frozenset[str], progress: Progress,
    *, max_attempts: int = 3,
) -> list[str]:
    tool = {"type": "function", "function": {
        "name": "select_listing_pages",
        "description": "Select supplied recruitment listing URLs. Invalid arguments receive correction feedback.",
        "parameters": {"type": "object", "required": ["urls"], "additionalProperties": False,
                       "properties": {"urls": {"type": "array", "items": {"type": "string", "enum": sorted(allowed)}}}},
    }}
    initial: dict[str, Any] = {"role": "user", "content": prompt}
    messages = [initial]
    error = "Call select_listing_pages with urls, an array of supplied URLs; use [] only if none qualify."
    for attempt in range(max_attempts):
        await progress.check_stop()
        if progress.should_stop:
            raise RetryInterruptedError("stopped during discovery selection")
        try:
            message = await model.complete_with_tools(messages, [tool])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise  # transport/rate-limit failures belong to durable retries
            # A malformed native tool call can be rejected by Ollama itself.
            error = "The model server rejected the tool arguments. " + error
            messages = [initial, {"role": "user", "content": error}]
            continue
        calls = message.get("tool_calls") or []
        if not calls:
            # Do not retain unbounded reasoning or treat text JSON as a tool call.
            messages = [initial, {"role": "user", "content": error}]
            continue
        messages.append(message)
        selected: list[str] | None = None
        valid_single_call = len(calls) == 1
        for call in calls:
            try:
                function = call.get("function") or {}
                if function.get("name") != "select_listing_pages" or not valid_single_call:
                    raise ValueError("call select_listing_pages exactly once")
                raw = function.get("arguments")
                args = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(args, dict) or set(args) != {"urls"}:
                    raise ValueError("arguments must be an object containing only urls")
                urls = args["urls"]
                if not isinstance(urls, list) or any(not isinstance(u, str) for u in urls):
                    raise ValueError("urls must be an array of strings")
                if any(u not in allowed for u in urls):
                    raise ValueError("every URL must exactly match a supplied candidate; invented URLs are forbidden")
                selected = list(dict.fromkeys(urls))
                error = "accepted"
            except (ValueError, TypeError) as exc:
                error = f"Validation failed: {exc}. Correct the arguments and call select_listing_pages again."
            feedback: dict[str, Any] = {"role": "tool", "content": error}
            if call.get("id"):
                feedback["tool_call_id"] = call["id"]
            else:
                feedback["tool_name"] = function.get("name") or "select_listing_pages"
            messages.append(feedback)
        if selected is not None:
            _LOGGER.info("discovery selection accepted on attempt %s (%s URLs)", attempt + 1, len(selected))
            return selected
        _LOGGER.warning("discovery selection attempt %s: %s", attempt + 1, error)
    raise DiscoverySelectionExhaustedError(f"discovery selection failed after {max_attempts} tool attempts: {error}")
