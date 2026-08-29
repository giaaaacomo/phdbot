"""Stadio 3: genera con l'LLM (una tantum) lo schema CSS di estrazione per listing page."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig
from crawl4ai import LLMConfig as C4ALLMConfig
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy
from crawl4ai.models import CrawlResult
from crawl4ai.utils import preprocess_html_for_schema
from injector import Injector
from litellm import acompletion
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.config import Settings
from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.university import University
from phd_searcher.pipeline.progress import Progress
from phd_searcher.pipeline.retry import retry_async
from phd_searcher.pipeline.schema_quality import (
    repair_base_anchor_url_schema,
    schema_quality_issues,
)
from phd_searcher.pipeline.urls import is_listing_page_url

_QUERY_UNIVERSITY = (
    "This page lists open PhD positions / doctoral vacancies. Extract the repeated "
    "vacancy items with fields: title (the position title), url (link to the vacancy "
    "detail page, only when the vacancy item really contains one; never use a site "
    "homepage, navigation, breadcrumb, header or footer link, and omit this field when "
    "the full vacancy is published inline), deadline (application deadline text, if shown), description "
    "(short summary text, if shown), area (research area/field, if shown), "
    "language (language of the announcement, if shown), duration (contract or PhD "
    "duration text, if shown), compensation (salary, stipend or funding text, including "
    "currency and payment period, if shown), published (publication or posting date text, if shown), "
    "position_type (the explicit opportunity type, if shown)."
    " Also extract research_group (laboratory, team, department or research group, if shown)."
)
_QUERY_AGGREGATOR = _QUERY_UNIVERSITY[:-1] + (
    ", institution (the university/organisation offering the position), country (country of the position, if shown)."
)

_SCHEMA_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_extraction_schema",
        "description": (
            "Submit a Crawl4AI CSS extraction schema. The application validates "
            "it against the real HTML and returns actionable errors when selectors fail."
        ),
        "parameters": {
            "type": "object",
            "required": ["name", "baseSelector", "fields"],
            "properties": {
                "name": {"type": "string"},
                "baseSelector": {"type": "string"},
                "baseFields": {"type": "array", "items": {"type": "object"}},
                "fields": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": False,
        },
    },
}

# A valid CSS extraction schema is normally a few hundred to a few thousand
# tokens.  Without a generation cap, a model that fails to call the tool can
# fill the whole 65k context, wasting roughly ten minutes and delaying a
# checkpointed Stop.  Keep the input context large for complex pages while
# bounding only the generated response.
_SCHEMA_NUM_PREDICT = 8192


class _ToolFunction(Protocol):
    arguments: object


class _ToolCall(Protocol):
    function: _ToolFunction


class SchemaGenerationExhaustedError(RuntimeError):
    """The model used its complete validation-feedback budget for this HTML."""


def _checkpoint_int(value: object) -> int:
    return int(value) if isinstance(value, (int, float, str)) else 0


def _should_stop(progress: Progress) -> bool:
    return progress.should_stop


def _schema_cache_key(url: str, kind: str) -> tuple[str, str] | None:
    """Scope schema reuse to the same host and listing kind.

    A schema is never trusted from the key alone: it still has to validate
    against the target page HTML before reuse.  The key only bounds the cheap
    candidate set and prevents cross-tenant/domain reputation leakage.
    """
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    if not host:
        return None
    return kind, host.casefold().rstrip(".")


def _validated_reusable_schema(
    html: str,
    schemas: list[dict[str, object]],
    *,
    expected_fields: list[str],
) -> dict[str, object] | None:
    """Return the first cached schema that passes the normal target-page gate."""
    for raw_schema in schemas:
        schema = repair_base_anchor_url_schema(raw_schema)
        if schema_quality_issues(schema):
            continue
        try:
            validation = JsonCssExtractionStrategy._validate_schema(
                schema,
                html,
                "CSS",
                expected_fields=expected_fields,
            )
        except (TypeError, ValueError):
            continue
        if validation["success"]:
            return schema
    return None


def _remember_schema(
    cache: dict[tuple[str, str], list[dict[str, object]]],
    key: tuple[str, str] | None,
    schema: dict[str, object],
) -> None:
    if key is None:
        return
    candidates = cache.setdefault(key, [])
    fingerprint = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    if all(
        json.dumps(item, sort_keys=True, separators=(",", ":")) != fingerprint
        for item in candidates
    ):
        candidates.append(deepcopy(schema))


def _tool_arguments(call: dict[str, object] | _ToolCall) -> dict[str, object]:
    if isinstance(call, dict):
        function = call.get("function")
        if not isinstance(function, dict):
            raise ValueError("tool call must contain a function object")
        raw = function.get("arguments")
    else:
        raw = call.function.arguments
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


async def _complete_with_tools(
    llm_config: C4ALLMConfig,
    messages: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[Any], bool]:
    """Usa Ollama nativo: il bridge LiteLLM perde i tool call di gpt-oss."""
    if llm_config.provider.startswith("ollama/") and llm_config.base_url:
        base_url = llm_config.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        payload = {
            "model": llm_config.provider.removeprefix("ollama/"),
            "messages": messages,
            "tools": [_SCHEMA_TOOL],
            "stream": False,
            "options": {
                "temperature": 0,
                "num_ctx": 65536,
                "num_predict": _SCHEMA_NUM_PREDICT,
            },
        }
        async with httpx.AsyncClient(timeout=600) as client:
            response = await client.post(f"{base_url}/api/chat", json=payload)
            response.raise_for_status()
        message = response.json().get("message") or {}
        return message, list(message.get("tool_calls") or []), True

    response = await acompletion(
        model=llm_config.provider,
        api_base=llm_config.base_url,
        api_key=llm_config.api_token,
        messages=messages,
        tools=[_SCHEMA_TOOL],
        tool_choice="required",
        temperature=0,
    )
    raw_message = response.choices[0].message
    message = {
        "role": "assistant",
        "content": raw_message.content or "",
        "tool_calls": [c.model_dump(exclude_none=True) for c in raw_message.tool_calls or []],
    }
    return message, list(raw_message.tool_calls or []), False


async def _generate_schema_with_tools(
    html: str,
    query: str,
    llm_config: C4ALLMConfig,
    *,
    expected_fields: list[str] | None = None,
    max_attempts: int = 4,
) -> dict[str, object]:
    """Genera e valida lo schema con tool feedback, senza structured output."""
    compact_html = preprocess_html_for_schema(
        html_content=html,
        text_threshold=2000,
        attr_value_threshold=500,
        max_size=500_000,
    )
    prompt = JsonCssExtractionStrategy._build_schema_prompt(compact_html, "CSS", query) + (
        "\n\nDo not write the schema in the message content. You MUST call "
        "`submit_extraction_schema` with the complete schema as its arguments."
    )
    initial_message: dict[str, Any] = {"role": "user", "content": prompt}
    messages: list[dict[str, Any]] = [initial_message]
    last_error = "the model did not call the schema tool"

    for attempt in range(1, max_attempts + 1):
        try:
            message, calls, native_ollama = await _complete_with_tools(llm_config, messages)
        except httpx.HTTPStatusError as exc:
            transport_error = (
                f"Ollama rejected the tool response: HTTP {exc.response.status_code}: {exc.response.text[:500]}"
            )
            print(f"schema tool attempt {attempt}: {transport_error}")
            # Harmony può fallire nel serializzare conversazioni tool lunghe.
            # Riparte pulito conservando il feedback di validazione precedente.
            messages = [
                initial_message,
                {
                    "role": "user",
                    "content": f"Previous validation feedback:\n{last_error}\n\n{transport_error}\nCall the tool again.",
                },
            ]
            last_error = transport_error
            continue
        if not calls:
            messages.append({"role": "assistant", "content": message.get("content") or ""})
            last_error = "No tool call was produced. Call submit_extraction_schema."
            messages.append({"role": "user", "content": last_error})
            continue

        call = calls[0]
        messages.append(message)
        try:
            schema = repair_base_anchor_url_schema(_tool_arguments(call))
            # Il titolo è indispensabile. Il link non lo è: diverse università
            # pubblicano le vacancy per esteso nella listing senza detail page.
            validation = JsonCssExtractionStrategy._validate_schema(
                schema,
                html,
                "CSS",
                expected_fields=expected_fields or ["title", "url"],
            )
            structural_issues = schema_quality_issues(schema) if validation["success"] else ()
            if validation["success"] and not structural_issues:
                print(
                    f"schema tool attempt {attempt}: accepted "
                    f"({validation['base_elements_found']} items, "
                    f"{validation['populated_fields']}/{validation['total_fields']} populated fields)"
                )
                return schema
            if structural_issues:
                last_error = (
                    "The schema is structurally unsafe: "
                    f"{', '.join(structural_issues)}. Select the repeated vacancy cards/rows; "
                    "never use every document element or site navigation as the item container."
                )
                print(f"schema tool attempt {attempt}: rejected ({', '.join(structural_issues)})")
            else:
                print(
                    f"schema tool attempt {attempt}: rejected "
                    f"({validation['base_elements_found']} items, "
                    f"{validation['populated_fields']}/{validation['total_fields']} populated fields)"
                )
                last_error = JsonCssExtractionStrategy._build_feedback_message(
                    validation,
                    schema,
                    attempt,
                    False,
                )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            last_error = f"The tool arguments are invalid: {exc}. Pass a complete valid schema."

        if native_ollama:
            messages.append(
                {
                    "role": "tool",
                    "tool_name": "submit_extraction_schema",
                    "content": last_error,
                }
            )
        else:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": "submit_extraction_schema",
                    "content": last_error,
                }
            )

    raise SchemaGenerationExhaustedError(
        f"schema tool failed after {max_attempts} attempts: {last_error}"
    )


async def run(
    container: Injector,
    *,
    limit: int | None = None,
    name_like: str | None = None,
    progress: Progress | None = None,
) -> int:
    """Ritorna il numero di schemi passati a `ok` (progresso, non tentativi)."""
    progress = progress or Progress()
    settings = container.get(Settings)
    session_maker = container.get(async_sessionmaker[AsyncSession])
    llm_config = C4ALLMConfig(  # crawl4ai usa litellm internamente: stessi model string / api_base
        provider=settings.llm.model,
        # crawl4ai sostituisce il provider con openai/gpt-4o se api_token è falsy: mai passare stringa vuota
        api_token=settings.llm.api_key or "no-token-needed",
        base_url=settings.llm.api_base,
    )
    crawl_config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, check_robots_txt=True)
    checkpoint = await progress.load_checkpoint()
    generated = _checkpoint_int(checkpoint.get("generated", 0))
    reused = _checkpoint_int(checkpoint.get("reused", 0))
    processed = _checkpoint_int(checkpoint.get("processed", 0))
    remaining = None if limit is None else max(limit - processed, 0)

    async with session_maker() as session:
        # Classifica anche righe storiche scoperte prima dell'introduzione del filtro.
        existing_pages = (await session.execute(select(ListingPage))).scalars().all()
        unsupported = [page for page in existing_pages if not is_listing_page_url(page.url)]
        for page in unsupported:
            page.schema_status = "unsupported"
        if unsupported:
            await session.commit()
            print(f"schema_gen: marked {len(unsupported)} document URLs unsupported")

        reusable_schemas: dict[tuple[str, str], list[dict[str, object]]] = {}
        for existing in existing_pages:
            if existing.schema_status != "ok" or not isinstance(existing.extraction_schema, dict):
                continue
            _remember_schema(
                reusable_schemas,
                _schema_cache_key(existing.url, existing.kind),
                existing.extraction_schema,
            )

        stmt = (
            select(ListingPage, University)
            .outerjoin(University, ListingPage.university_id == University.id)
            .where(ListingPage.schema_status.in_(("missing", "stale", "failed")))
            .order_by(
                case((ListingPage.kind == "aggregator", 0), else_=1),
                case((ListingPage.schema_status == "missing", 0), else_=1),
                func.coalesce(University.sitelinks, 0).desc(),
            )
        )
        if name_like:
            stmt = stmt.where(University.name.ilike(f"%{name_like}%"))
        if remaining is not None:
            stmt = stmt.limit(remaining)
        rows = (await session.execute(stmt)).all()
        await progress.begin(len(rows))

        async with AsyncWebCrawler() as crawler:
            for page, uni in rows:
                if _should_stop(progress):
                    break
                await progress.tick(uni.name if uni else page.url)
                if progress.should_stop:
                    break
                try:
                    async def crawl_listing(listing_url: str = page.url) -> CrawlResult:
                        result = await crawler.arun(listing_url, config=crawl_config)
                        if not result.success or not result.html:
                            raise RuntimeError(result.error_message or "empty page")
                        return result

                    result = await retry_async(progress, f"schema:{page.id}:crawl", crawl_listing)
                    query = _QUERY_AGGREGATOR if page.kind == "aggregator" else _QUERY_UNIVERSITY
                    html = result.cleaned_html or result.html
                    expected_fields = (
                        ["title", "url", "deadline", "institution", "country"]
                        if page.kind == "aggregator"
                        else ["title"]
                    )
                    cache_key = _schema_cache_key(page.url, page.kind)
                    schema = _validated_reusable_schema(
                        html,
                        reusable_schemas.get(cache_key, []) if cache_key is not None else [],
                        expected_fields=expected_fields,
                    )
                    if schema is not None:
                        reused += 1
                        print(f"schema_gen: reused a target-validated schema for {page.url}")
                    else:

                        async def generate_schema(
                            current_html: str = html,
                            current_query: str = query,
                            current_expected_fields: list[str] = expected_fields,
                        ) -> dict[str, object]:
                            return await _generate_schema_with_tools(
                                current_html,
                                current_query,
                                llm_config,
                                expected_fields=current_expected_fields,
                            )

                        schema = await retry_async(
                            progress,
                            f"schema:{page.id}:tool",
                            generate_schema,
                            # The inner loop has already shown the model every
                            # validation error up to four times. Replaying that
                            # complete deterministic exchange cannot repair the
                            # page; only transport failures should restart it.
                            non_retryable=(SchemaGenerationExhaustedError,),
                        )
                    page.extraction_schema = schema
                    page.schema_status = "ok"
                    _remember_schema(reusable_schemas, cache_key, schema)
                    generated += 1
                except Exception as exc:
                    if _should_stop(progress):
                        break
                    print(f"schema_gen failed for {page.url}: {exc}")
                    # Qui gli errori arrivano da crawling/LLM: non sono ancora state
                    # eseguite scritture DB. Un rollback scadrebbe tutte le righe ORM
                    # caricate e il successivo accesso a `page` causerebbe MissingGreenlet.
                    page.schema_status = "failed"
                await session.commit()
                processed += 1
                await progress.save_checkpoint(
                    processed=processed,
                    generated=generated,
                    reused=reused,
                    last_listing_page_id=page.id,
                )
    print(f"schema_gen: {generated} ok, {reused} reused ({len(rows)} attempted)")
    return generated
