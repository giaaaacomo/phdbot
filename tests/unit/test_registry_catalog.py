import io
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from phd_searcher.database.models.university import University
from phd_searcher.pipeline.institutions import _build_entities
from phd_searcher.pipeline.registry_catalog import (
    EUROPE,
    RegistryRecord,
    candidate,
    import_snapshot,
    objects,
    website_key,
)


def row(identifier="00m3mb357", *, qids=(), country="FR", relationships=(), **changes):
    return {
        "id": f"https://ror.org/{identifier}",
        "status": "active",
        "names": [{"value": f"Example research lab {identifier}", "types": ["ror_display"]}],
        "types": ["facility"],
        "links": [{"type": "website", "value": f"https://example.org/{identifier}"}],
        "locations": [{"geonames_details": {"country_code": country}}],
        "relationships": list(relationships),
        "external_ids": [{"type": "wikidata", "all": list(qids)}],
        **changes,
    }


def session_for(existing=()):
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = list(existing)
    session.scalars.return_value = result
    session.add = MagicMock(side_effect=lambda uni: setattr(uni, "id", 999))
    return session


def test_streaming_parser_and_scope():
    assert list(objects(io.StringIO('[ {"text":"日本語 ' + "x" * 70000 + '"}, {} ]')))[1] == {}
    assert list(objects(io.StringIO("[]"))) == []
    assert "SG" not in EUROPE
    assert "GE" not in EUROPE
    assert {"IT", "GB", "SM", "CH"} <= EUROPE
    assert website_key("https://www.example.org/lab/") == "example.org/lab"
    assert website_key("http://example.org/other") != "example.org/lab"
    assert not website_key("https://password@example.org/")


@pytest.mark.parametrize(
    "payload", ["", "{}", "[{}", "[{},]", "[{} {}]", "[3]", "[] junk", "[" + "x" * (4 * 1024 * 1024)]
)
def test_invalid_snapshot_is_rejected(payload):
    with pytest.raises(ValueError):  # noqa: PT011 -- several structural validation errors
        list(objects(io.StringIO(payload)))


def test_explicit_cross_border_relation_not_global_expansion():
    parent = "https://ror.org/02feahw73"
    data = RegistryRecord.model_validate(row(country="SG", relationships=[{"id": parent, "type": "parent"}]))
    assert candidate(data, {"FR"}, set()) is None
    matched = candidate(data, {"FR"}, {parent})
    assert matched["country"] == "SG"
    assert matched["scope"] == "explicit_parent_relationship"
    data.status = "inactive"
    assert candidate(data, {"FR"}, {parent}) is None


def test_catalog_only_does_not_generate_institution_embeddings():
    uni = University(id=1, name="Known but not activated", country="FR", discovery_status="catalogued")
    assert _build_entities([uni], [], name_like=None) == []


@pytest.mark.asyncio
async def test_import_does_not_requeue_existing_and_no_fake_qids(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps([row(qids=["Q123"]), row("05hqw3605")]))
    uni = University(
        id=1,
        wikidata_id="Q123",
        name="Audited name",
        country="FR",
        website_url="https://audited.example",
        discovery_status="done",
    )
    session = session_for([uni])
    result = await import_snapshot(session, path, countries={"FR"}, parents=set(), apply=True)
    assert result["counts"]["linked_or_updated"] == 1
    assert uni.discovery_status == "done"
    assert uni.website_url == "https://audited.example"
    assert uni.name == "Audited name"
    added = session.add.call_args.args[0]
    assert added.wikidata_id is None
    assert added.discovery_status == "catalogued"
    assert added.registry_metadata["record"]["relationships"] == []
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_shared_site_is_conflict_not_merge_and_dry_run_rolls_back(tmp_path):
    path = tmp_path / "snapshot.json"
    data = row()
    path.write_text(json.dumps([data]))
    uni = University(id=1, name="Another name", website_url=data["links"][0]["value"], country="FR")
    session = session_for([uni])
    result = await import_snapshot(session, path, countries={"FR"}, parents=set())
    assert result["counts"]["conflicts"] == 1
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_two_ror_ids_for_one_qid_conflict_even_in_preview(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps([row(qids=["Q123"]), row("05hqw3605", qids=["Q123"])]))
    session = session_for(
        [University(id=1, wikidata_id="Q123", name="Old name", country="FR", website_url="https://old.example")]
    )
    result = await import_snapshot(session, path, countries={"FR"}, parents=set())
    assert result["counts"]["linked_or_updated"] == 1
    assert result["counts"]["conflicts"] == 1
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_and_separate_activation_of_previously_catalogued(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps([row(), row("05hqw3605")]))
    session = session_for()
    result = await import_snapshot(session, path, countries={"FR"}, parents=set(), apply=True, max_new=1)
    assert result["counts"]["new_candidates"] == 2
    assert result["counts"]["catalogued"] == 1
    existing = session.add.call_args.args[0]
    session = session_for([existing])
    result = await import_snapshot(
        session, path, countries={"FR"}, parents=set(), apply=True, max_new=0, activate_limit=1
    )
    assert result["counts"]["activated"] == 1
    assert existing.discovery_status == "pending"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_corrupt_or_duplicate_tail_fails_before_writes(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps([row(), row()]))
    session = session_for()
    with pytest.raises(ValueError, match="duplicate ROR"):
        await import_snapshot(session, path, countries={"FR"}, parents=set(), apply=True)
    session.execute.assert_not_awaited()
    session.add.assert_not_called()
