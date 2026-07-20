"""Ricerca web per la discovery: ddg (ddgs, senza chiave) o brave (API key)."""

from __future__ import annotations

import asyncio

import httpx

from phd_searcher.config.search import SearchConfig

_PHD_TERMS = 'phd OR doctoral OR "open positions" OR vacancies'


def _ddg_search(query: str, max_results: int) -> list[str]:
    from ddgs import DDGS  # import locale: opzionale a runtime

    with DDGS() as ddgs:
        return [r["href"] for r in ddgs.text(query, max_results=max_results) if r.get("href")]


async def _brave_search(query: str, max_results: int, api_key: str) -> list[str]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        )
        resp.raise_for_status()
        return [r["url"] for r in resp.json().get("web", {}).get("results", []) if r.get("url")]


async def search_listing_candidates(config: SearchConfig, domain: str) -> list[str]:
    """URL candidati per le pagine bandi di un dominio. Mai un'eccezione: [] su errore."""
    query = f"site:{domain} {_PHD_TERMS}"
    try:
        if config.provider == "brave" and config.api_key:
            return await _brave_search(query, config.max_results, config.api_key)
        if config.provider == "ddg":
            return await asyncio.to_thread(_ddg_search, query, config.max_results)  # ddgs è sincrona
    except Exception as exc:  # provider flaky: la discovery prosegue col solo imbuto
        print(f"search provider failed for {domain}: {exc}")
    return []
