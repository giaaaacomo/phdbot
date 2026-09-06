from datetime import date
from typing import ClassVar

import pytest

from phd_searcher.pipeline import source_adapters
from phd_searcher.pipeline.normalize import normalize_item
from phd_searcher.pipeline.source_adapters import (
    fetch_source_adapter,
    normalize_source_item_formats,
    source_adapter_name,
    talentadore_items,
    talentlink_items,
)


def test_source_declared_us_dates_are_not_guessed_as_european() -> None:
    items = normalize_source_item_formats(
        [{"published": "8/11/2026", "deadline": "9/14/2026 11:55 PM"}],
        {"dateFormats": {"published": "%m/%d/%Y", "deadline": "%m/%d/%Y"}},
    )

    assert items == [
        {
            "published": "8/11/2026",
            "deadline": "9/14/2026 11:55 PM",
            "__phdbot_published_date": "2026-08-11",
            "__phdbot_deadline_date": "2026-09-14",
        }
    ]
    normalized = normalize_item(items[0] | {"title": "Postdoc"}, base_url="https://example.test/jobs")
    assert normalized is not None
    assert normalized.published_raw == "8/11/2026"
    assert normalized.published_at == date(2026, 8, 11)
    assert normalized.deadline_raw == "9/14/2026 11:55 PM"
    assert normalized.deadline == date(2026, 9, 14)


def test_talentlink_adapter_maps_official_job_fields() -> None:
    schema = {
        "adapter": "talentlink",
        "apiHost": "https://jobs.example",
        "siteTechId": "SITE",
        "language": "en_GB",
        "salaryField": "SALARY",
        "departmentFields": ["DEPARTMENT"],
    }
    payload = {
        "jobs": [
            {
                "id": 42,
                "jobFields": {
                    "id": 42,
                    "SJOBTITLE": "Research Associate in Spatial Computing",
                    "applicationUrl": "https://jobs.example/apply/42",
                    "DPOSTINGSTART": 1788134400000,
                    "DPOSTINGEND": 1788739200000,
                    "SALARY": "£45,000 - £55,000 per year",
                    "DEPARTMENT": "Faculty of Engineering",
                },
                "customFields": [
                    {
                        "title": "About the role",
                        "content": "<p>This is a contract for 24 months.</p>",
                    }
                ],
            }
        ]
    }

    items = talentlink_items(payload, schema)

    assert source_adapter_name(schema) == "talentlink"
    assert items == [
        {
            "title": "Research Associate in Spatial Computing",
            "url": "https://jobs.example/apply/42",
            "description": "About the role\nThis is a contract for 24 months.",
            "area": "Faculty of Engineering",
            "research_group": "Faculty of Engineering",
            "deadline": "2026-09-07",
            "published": "2026-08-31",
            "compensation": "£45,000 - £55,000 per year",
            "duration": "This is a contract for 24 months.",
            "language": "en",
        }
    ]


def test_talentadore_adapter_preserves_rich_text_and_dates() -> None:
    payload = {
        "jobs": [
            {
                "name": "Doctoral Researcher in Interaction Design",
                "link": "https://ats.example/apply/abc",
                "description_text": "A funded 3 year position.\nSalary: EUR 2,800 per month",
                "business_unit_name": "Department of Computing",
                "start_date": "2026-08-25T08:00:00Z",
                "due_date": "2026-09-08T20:59:00Z",
            }
        ]
    }

    items = talentadore_items(payload)

    assert items[0]["title"] == "Doctoral Researcher in Interaction Design"
    assert items[0]["deadline"] == "2026-09-08T20:59:00Z"
    assert items[0]["published"] == "2026-08-25T08:00:00Z"
    assert items[0]["compensation"] == "Salary: EUR 2,800 per month"
    assert items[0]["research_group"] == "Department of Computing"


def test_currency_is_not_duplicated_when_salary_has_a_symbol() -> None:
    schema = {
        "apiHost": "https://emea3.recruitmentplatform.com",
        "siteTechId": "SITE",
        "salaryCurrency": "GBP",
        "salaryField": "SALARY",
    }
    payload = {
        "jobs": [
            {
                "id": 1,
                "jobFields": {
                    "id": 1,
                    "SJOBTITLE": "Researcher",
                    "SALARY": "£45,000 per year",
                },
            }
        ]
    }

    assert talentlink_items(payload, schema)[0]["compensation"] == "£45,000 per year"


def test_talentlink_restores_a_stripped_salary_range() -> None:
    schema = {
        "apiHost": "https://emea3.recruitmentplatform.com",
        "siteTechId": "SITE",
        "salaryCurrency": "GBP",
        "salaryField": "SALARY",
    }
    payload = {
        "jobs": [
            {
                "id": 1,
                "jobFields": {
                    "id": 1,
                    "SJOBTITLE": "Researcher",
                    "SALARY": "45399  59484 per annum",
                },
            }
        ]
    }

    item = talentlink_items(payload, schema)[0]
    normalized = normalize_item(item, base_url="https://example.test/jobs")

    assert item["compensation"] == "GBP 45399 - 59484 per annum"
    assert normalized is not None
    assert normalized.compensation_min == 45399
    assert normalized.compensation_max == 59484


def test_adapter_fails_closed_when_too_many_jobs_are_malformed() -> None:
    schema = {
        "apiHost": "https://emea3.recruitmentplatform.com",
        "siteTechId": "SITE",
    }
    payload = {
        "jobs": [
            {"id": 1, "jobFields": {"id": 1, "SJOBTITLE": "Researcher"}},
            {"id": 2, "jobFields": {"id": 2}},
        ]
    }

    with pytest.raises(RuntimeError, match="accepted 1/2"):
        talentlink_items(payload, schema)


def test_talentadore_resolves_relative_job_links() -> None:
    payload = {"jobs": [{"name": "Doctoral researcher", "link": "/positions/abc"}]}

    items = talentadore_items(payload, base_url="https://ats.talentadore.com/feed.json")

    assert items[0]["url"] == "https://ats.talentadore.com/positions/abc"


def test_unknown_adapter_is_not_silently_executed() -> None:
    with pytest.raises(RuntimeError, match="unsupported source adapter"):
        source_adapter_name({"adapter": "mystery"})


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class _FakeClient:
    init_kwargs: ClassVar[dict[str, object]] = {}
    post_kwargs: ClassVar[dict[str, object]] = {}
    payload: ClassVar[object] = {}

    def __init__(self, **kwargs: object) -> None:
        type(self).init_kwargs = kwargs

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, _url: str, **kwargs: object) -> _FakeResponse:
        type(self).post_kwargs = kwargs
        return _FakeResponse(type(self).payload)


@pytest.mark.asyncio
async def test_fetch_talentlink_dispatches_the_requested_page(monkeypatch) -> None:
    _FakeClient.payload = {
        "globals": {"jobsCount": 101},
        "jobs": [{"id": 101, "jobFields": {"id": 101, "SJOBTITLE": "Doctoral researcher"}}],
    }
    monkeypatch.setattr(source_adapters.httpx, "AsyncClient", _FakeClient)

    items = await fetch_source_adapter(
        {
            "adapter": "talentlink",
            "apiHost": "https://emea3.recruitmentplatform.com",
            "siteTechId": "SITE",
            "pageSize": 100,
        },
        page_number=1,
    )

    assert items is not None
    assert len(items) == 1
    assert _FakeClient.post_kwargs["params"]["firstResult"] == 100  # type: ignore[index]
    assert _FakeClient.init_kwargs["follow_redirects"] is False


@pytest.mark.asyncio
async def test_fetch_talentlink_rejects_an_incomplete_page(monkeypatch) -> None:
    _FakeClient.payload = {
        "globals": {"jobsCount": 2},
        "jobs": [{"id": 1, "jobFields": {"id": 1, "SJOBTITLE": "Researcher"}}],
    }
    monkeypatch.setattr(source_adapters.httpx, "AsyncClient", _FakeClient)

    with pytest.raises(RuntimeError, match="received 1/2"):
        await fetch_source_adapter(
            {
                "adapter": "talentlink",
                "apiHost": "https://emea3.recruitmentplatform.com",
                "siteTechId": "SITE",
            },
            page_number=0,
        )


@pytest.mark.asyncio
async def test_fetch_talentlink_accepts_its_keyless_empty_page(monkeypatch) -> None:
    _FakeClient.payload = {"globals": {"jobsCount": 0, "selectedSearchCriteria": []}}
    monkeypatch.setattr(source_adapters.httpx, "AsyncClient", _FakeClient)

    items = await fetch_source_adapter(
        {
            "adapter": "talentlink",
            "apiHost": "https://emea3.recruitmentplatform.com",
            "siteTechId": "SITE",
        },
        page_number=1,
    )

    assert items == []


@pytest.mark.asyncio
async def test_fetch_adapter_rejects_untrusted_hosts() -> None:
    with pytest.raises(RuntimeError, match="untrusted talentadore feedUrl URL"):
        await fetch_source_adapter(
            {"adapter": "talentadore", "feedUrl": "http://127.0.0.1/private"},
            page_number=0,
        )
