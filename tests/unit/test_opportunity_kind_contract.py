from importlib import import_module

from sqlalchemy import String

from phd_searcher.database.models.position import Position
from phd_searcher.opportunity_kinds import (
    DEFAULT_OPPORTUNITY_KIND,
    OPPORTUNITY_KINDS,
    normalize_opportunity_kind,
)
from phd_searcher.typedef.search import PositionDetail, ScreeningItem, SearchHit


def test_opportunity_kind_taxonomy_is_closed_and_normalized():
    assert {
        "unknown",
        "vacancy",
        "programme",
        "spontaneous",
        "information",
    } == OPPORTUNITY_KINDS
    assert DEFAULT_OPPORTUNITY_KIND == "unknown"
    assert normalize_opportunity_kind(" Programme ") == "programme"
    assert normalize_opportunity_kind("not-a-kind") == "unknown"


def test_position_opportunity_kind_column_contract():
    column = Position.__table__.columns["opportunity_kind"]

    assert isinstance(column.type, String)
    assert column.type.length == 32
    assert column.nullable is False
    assert column.default is not None
    assert column.default.arg == "unknown"
    assert column.server_default is not None
    assert str(column.server_default.arg) == "unknown"


def test_search_hit_exposes_opportunity_kind_with_safe_default():
    hit = SearchHit(
        position_id=1,
        score=1.0,
        title="Doctoral researcher",
        university="Example University",
        country="IT",
        url="https://example.test/jobs/1",
    )

    assert hit.opportunity_kind == "unknown"
    assert SearchHit(**{**hit.model_dump(), "opportunity_kind": "vacancy"}).opportunity_kind == "vacancy"


def test_position_payload_types_expose_opportunity_kind():
    assert PositionDetail.model_fields["opportunity_kind"].default == "unknown"
    assert ScreeningItem.model_fields["opportunity_kind"].default == "unknown"


def test_opportunity_kind_migration_contract(monkeypatch):
    migration = import_module(
        "phd_searcher.database.alembic.versions.f7c2a18d9b30_position_opportunity_kind"
    )
    added: list[tuple[str, object]] = []
    statements: list[str] = []
    dropped: list[tuple[str, str]] = []
    monkeypatch.setattr(migration.op, "add_column", lambda table, column: added.append((table, column)))
    monkeypatch.setattr(migration.op, "execute", lambda statement: statements.append(str(statement)))
    monkeypatch.setattr(migration.op, "drop_column", lambda table, column: dropped.append((table, column)))

    migration.upgrade()

    assert migration.down_revision == "e6a4b9c2d170"
    assert len(added) == 1
    table, column = added[0]
    assert table == "positions"
    assert column.name == "opportunity_kind"
    assert isinstance(column.type, String)
    assert column.type.length == 32
    assert column.nullable is False
    assert column.server_default is not None
    assert str(column.server_default.arg) == "unknown"
    assert "SET opportunity_kind = 'vacancy'" in statements[0]
    assert "WHERE screening_status = 'eligible'" in statements[0]
    assert "indexed_at" not in statements[0]

    migration.downgrade()

    assert dropped == [("positions", "opportunity_kind")]
