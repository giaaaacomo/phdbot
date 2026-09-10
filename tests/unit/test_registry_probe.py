import asyncio
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from phd_searcher.pipeline.registry_probe import _cooldown, probe_relations, ror_id

SEED = "https://ror.org/02feahw73"


def record(number=1, **changes):
    # Synthetic format-valid IDs: no claim these are real registry identities.
    return {
        "id": f"https://ror.org/0{number:06d}01",
        "status": "active",
        "names": [{"value": "A joint lab", "types": ["ror_display"]}, {"value": "LAB", "types": ["acronym"]}],
        "types": ["facility"],
        "links": [{"type": "website", "value": "https://lab.example/"}],
        "relationships": [{"id": SEED, "type": "parent"}],
        "locations": [{"geonames_details": {"country_code": "SG"}}],
        **changes,
    }


def transport_for(items, total=None):
    def handle(request):
        assert request.url.host == "api.ror.org"
        assert request.url.params["query.advanced"] == "relationships.id:https\\:\\/\\/ror.org\\/02feahw73"
        assert "query" not in request.url.params  # No name-based search.
        return httpx.Response(200, json={"number_of_results": len(items) if total is None else total, "items": items})

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_dynamic_identity_and_offline_cache(tmp_path):
    path = tmp_path / "cache.sqlite3"
    cold = await probe_relations(SEED, cache_path=path, country="sg", transport=transport_for([record()]))
    assert cold.state == "complete"
    assert cold.network_requests == 1
    assert cold.candidates[0]["aliases"] == ["LAB"]
    assert cold.candidates[0]["headquarters_countries"] == ["SG"]
    assert cold.candidates[0]["disposition"] == "identity_candidate_not_imported"

    def forbidden(request):
        pytest.fail("warm preview must not contact registry or candidate websites")

    warm = await probe_relations(
        SEED, cache_path=path, country="SG", max_requests=0, transport=httpx.MockTransport(forbidden)
    )
    assert warm.state == "complete"
    assert warm.cache_hits == 1
    assert warm.network_requests == 0
    assert warm.candidates == cold.candidates


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "inactive"},
        {"status": "withdrawn"},
        {"id": SEED},
        {"relationships": [{"id": SEED, "type": "child"}]},
        {"relationships": [{"id": SEED, "type": "child"}, {"id": "https://ror.org/00m3mb357", "type": "parent"}]},
        {"locations": [{"geonames_details": {"country_code": "FR"}}]},
        {"links": [{"type": "website", "value": "https://secret@lab.example/"}]},
        {"links": [{"type": "website", "value": "file:///tmp/data"}]},
        {"links": []},
        {"names": [{"value": "Unlabelled", "types": ["alias"]}]},
    ],
)
@pytest.mark.asyncio
async def test_local_relationship_status_and_identity_checks(tmp_path, changes):
    result = await probe_relations(
        SEED, cache_path=tmp_path / "cache", country="SG", transport=transport_for([record(**changes)])
    )
    assert result.state == "complete"
    assert result.candidates == []


@pytest.mark.asyncio
async def test_pagination_budget_and_explicit_resume(tmp_path):
    def handle(request):
        page = int(request.url.params["page"])
        rows = [record(n) for n in range(1, 21)] if page == 1 else [record(21)]
        return httpx.Response(200, json={"number_of_results": 21, "items": rows})

    transport = httpx.MockTransport(handle)
    first = await probe_relations(SEED, cache_path=tmp_path / "cache", max_pages=1, transport=transport)
    assert first.state == "partial"
    assert first.reason == "page_budget"
    assert first.next_page == 2
    second = await probe_relations(SEED, cache_path=tmp_path / "cache", start_page=first.next_page, transport=transport)
    assert second.state == "complete"
    assert second.next_page is None
    assert len(first.candidates) + len(second.candidates) == 21


@pytest.mark.parametrize("status", [429, 503, 403, 301])
@pytest.mark.asyncio
async def test_error_cooldown_is_durable_and_redirect_not_followed(tmp_path, status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "7200", "Location": "https://unrelated.example/"})

    for _ in range(2):
        result = await probe_relations(SEED, cache_path=tmp_path / "cache", transport=httpx.MockTransport(handle))
        assert result.state == "deferred"
        assert result.next_page == 1
    assert len(calls) == 1
    assert result.cache_hits == 1


@pytest.mark.parametrize(("items", "total"), [([], 1), ([record()], 20), ([record(), record()], 2)])
@pytest.mark.asyncio
async def test_incomplete_or_duplicate_page_not_cached_as_success(tmp_path, items, total):
    result = await probe_relations(SEED, cache_path=tmp_path / "cache", transport=transport_for(items, total))
    assert result.state == "failed"
    assert result.pages_read == 0
    assert result.next_page == 1
    retry = await probe_relations(SEED, cache_path=tmp_path / "cache", max_requests=0)
    assert retry.cache_hits == 0


@pytest.mark.asyncio
async def test_time_budget_and_request_budget(tmp_path):
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"items": [], "number_of_results": 0})

    result = await probe_relations(
        SEED, cache_path=tmp_path / "cache", seconds=0.01, transport=httpx.MockTransport(slow)
    )
    assert result.reason == "time_budget"
    assert result.next_page == 1
    result = await probe_relations(SEED, cache_path=tmp_path / "cache", max_requests=0)
    assert result.reason == "network_budget"
    assert result.network_requests == 0


@pytest.mark.parametrize("body", [b"not JSON", b'{"items": []}', b'{"items": [], "number_of_results": -1}'])
@pytest.mark.asyncio
async def test_invalid_response_is_failure_not_empty_catalog(tmp_path, body):
    result = await probe_relations(
        SEED,
        cache_path=tmp_path / "cache",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
    )
    assert result.state == "failed"
    assert result.next_page == 1


def test_identifiers_and_retry_after(monkeypatch):
    assert ror_id("02feahw73") == SEED
    with pytest.raises(ValueError, match="ROR ID"):
        ror_id("https://attacker.example/02feahw73")
    monkeypatch.setattr("phd_searcher.pipeline.registry_probe.time.time", lambda: 0)
    assert _cooldown("7200") == 7200
    assert _cooldown(format_datetime(datetime.fromtimestamp(7200, UTC), usegmt=True)) == 7200
    assert _cooldown("invalid") == 3600
