"""Health-endpoint integration."""

from __future__ import annotations


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
