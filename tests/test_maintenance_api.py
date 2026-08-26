"""UC-6.10：``/v1/maintenance-windows`` CRUD。"""


def _body(**over):
    b = {
        "service": "svc-a",
        "start_at": "2026-08-26T11:00:00Z",
        "end_at": "2026-08-26T12:00:00Z",
        "reason": "scheduled release",
    }
    b.update(over)
    return b


def test_create_and_list(client):
    resp = client.post("/v1/maintenance-windows", json=_body())
    assert resp.status_code == 201
    assert resp.json()["id"] == 1
    items = client.get("/v1/maintenance-windows").json()["items"]
    assert len(items) == 1
    assert items[0]["service"] == "svc-a"


def test_list_filter_by_service(client):
    client.post("/v1/maintenance-windows", json=_body(service="svc-a"))
    client.post("/v1/maintenance-windows", json=_body(service="svc-b"))
    assert len(client.get("/v1/maintenance-windows", params={"service": "svc-b"}).json()["items"]) == 1


def test_update_and_delete(client):
    client.post("/v1/maintenance-windows", json=_body())
    updated = client.put("/v1/maintenance-windows/1", json={"reason": "hotfix"})
    assert updated.status_code == 200
    assert updated.json()["reason"] == "hotfix"
    assert client.put("/v1/maintenance-windows/999", json={"reason": "x"}).status_code == 404

    assert client.delete("/v1/maintenance-windows/1").status_code == 204
    assert client.get("/v1/maintenance-windows").json()["items"] == []


def test_isolated_by_tenant(client):
    client.post("/v1/maintenance-windows", json=_body())
    other = client.get("/v1/maintenance-windows", headers={"X-Tenant-Id": "tenant-b"}).json()["items"]
    assert other == []
