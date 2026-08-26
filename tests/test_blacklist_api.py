"""UC-6.11：``/v1/blacklist`` CRUD。"""


def _body(**over):
    b = {
        "domain": "application",
        "service": "svc-a",
        "signal": "cpu_usage",
        "reason": "known noisy",
    }
    b.update(over)
    return b


def test_create_and_list(client):
    resp = client.post("/v1/blacklist", json=_body())
    assert resp.status_code == 201
    assert resp.json()["id"] == 1
    items = client.get("/v1/blacklist").json()["items"]
    assert len(items) == 1
    assert items[0]["signal"] == "cpu_usage"


def test_update_enable_toggle(client):
    client.post("/v1/blacklist", json=_body())
    updated = client.put("/v1/blacklist/1", json={"enabled": False, "reason": "re-enabled after fix"})
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False
    assert client.put("/v1/blacklist/999", json={"enabled": True}).status_code == 404


def test_delete(client):
    client.post("/v1/blacklist", json=_body())
    assert client.delete("/v1/blacklist/1").status_code == 204
    assert client.get("/v1/blacklist").json()["items"] == []
