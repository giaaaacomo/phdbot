def test_health_ok(client):
    assert client.get("/health").status_code == 200
