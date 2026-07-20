"""Postgres-backed endpoints against the real stack (no LLM dependency)."""

from __future__ import annotations


def test_universities_coverage(client, seeded):
    r = client.get("/v1/universities")
    assert r.status_code == 200
    unis = r.json()["universities"]
    test_uni = next(u for u in unis if u["name"] == "Test University")
    assert test_uni["country"] == "IT"
    assert test_uni["positions_count"] == 1


def test_position_detail(client, seeded):
    unis = client.get("/v1/universities").json()["universities"]
    assert unis, "seed did not land"
    # find the seeded position id via a lookup sweep from 1 (single seeded row → id 1)
    r = client.get("/v1/positions/1")
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["position"]["title"] == "PhD in Testing"
    assert body["position"]["university"] == "Test University"


def test_position_not_found(client, seeded):
    r = client.get("/v1/positions/999999")
    assert r.status_code == 200
    assert r.json() == {"found": False, "position": None}
