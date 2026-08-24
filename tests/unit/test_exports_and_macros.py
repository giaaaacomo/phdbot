import json

import pytest

from phd_searcher.service.export_service import ExportService
from phd_searcher.typedef.macro import MacroCreate
from phd_searcher.typedef.search import SearchBody

PAYLOAD = {
    "title": "Test report",
    "generated_at": "2026-07-29T10:00:00+00:00",
    "search": {"query": "robotics"},
    "total": 1,
    "institutions": [],
    "hits": [
        {
            "position_id": 7,
            "score": 0.91,
            "title": "PhD <Robotics>",
            "university": "Example",
            "country": "IT",
            "position_type": "phd",
            "published_at": None,
            "first_seen_at": "2026-07-20T10:00:00+00:00",
            "last_seen_at": "2026-07-29T09:00:00+00:00",
            "deadline": "2099-01-01",
            "duration": "3 years",
            "compensation": "€2000/month",
            "compensation_max": 2000,
            "url": "https://example.test/7",
            "description": "Safe </script><script>alert(1)</script>",
        }
    ],
}


def test_standalone_html_embeds_data_without_closing_script():
    document = ExportService._html(PAYLOAD).decode()
    assert "PhD Searcher" not in document
    assert "<\\/script>" in document
    assert "Filter exported results" in document


def test_csv_and_json_exports_are_portable():
    csv_data = ExportService._csv(PAYLOAD).decode("utf-8-sig")
    json_data = json.loads(ExportService._json(PAYLOAD))
    assert "PhD <Robotics>" in csv_data
    assert "first_seen_at" in csv_data
    assert "2026-07-20T10:00:00+00:00" in csv_data
    assert json_data["hits"][0]["position_id"] == 7


def test_macro_requires_relative_destination_and_deduplicates_formats():
    macro = MacroCreate(
        name="Robotics",
        refresh=False,
        search=SearchBody(query="robotics"),
        export_formats=["html", "html", "csv"],
        destination="shared/robotics",
    )
    assert macro.export_formats == ["html", "csv"]
    with pytest.raises(ValueError, match="destination"):
        MacroCreate(
            name="Bad",
            search=SearchBody(query="robotics"),
            destination="../outside",
        )
