"""UC-6.6：``/v1/config`` 热加载 + 域规则读写。"""


def test_reload_returns_plugins(client):
    resp = client.post("/v1/config/reload")
    assert resp.status_code == 200
    data = resp.json()
    assert "plugins" in data
    assert len(data["plugins"]["collector"]) >= 1


def test_get_domain_config_from_seed(client):
    resp = client.get("/v1/config/application")
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] == "application"
    assert isinstance(data["config"]["detectors"], list)
    assert data["version"] >= 1


def test_list_domain_configs(client):
    resp = client.get("/v1/config")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "items should be non-empty (seeded application)"
    app = next((i for i in items if i["domain"] == "application"), None)
    assert app is not None
    assert "enabled" in app and "version" in app
    assert app["version"] >= 1


def test_get_domain_config_unknown_404(client):
    assert client.get("/v1/config/nonexistent").status_code == 404


def test_put_domain_config_bumps_version(client):
    body = {"detectors": [], "verify": {"persistence_rounds": 1}}
    resp = client.put("/v1/config/application", json=body)
    assert resp.status_code == 200
    v1 = resp.json()["version"]
    assert v1 >= 1
    got = client.get("/v1/config/application").json()
    assert got["config"]["verify"]["persistence_rounds"] == 1


def _put_custom_domain(client, domain="infra"):
    """建一个自定义域（upsert 即创建），返回 version。"""
    body = {"detectors": [{"signal": "cpu_usage", "plugin": "static_threshold", "params": {"threshold": 0.9}}]}
    resp = client.put(f"/v1/config/{domain}", json=body)
    assert resp.status_code == 200
    return resp.json()["version"]


def test_delete_domain_config(client):
    _put_custom_domain(client, "infra")
    resp = client.delete("/v1/config/infra")
    assert resp.status_code == 204
    assert client.get("/v1/config/infra").status_code == 404


def test_delete_domain_config_unknown_404(client):
    assert client.delete("/v1/config/nonexistent").status_code == 404


def test_delete_domain_config_blocked_when_referenced(client):
    """删被 enabled monitor_target 引用的域 → 400，reason 含 target_id。"""
    _put_custom_domain(client, "infra")
    # 建一个 domain=infra 的 target（POST /v1/monitors 落库）
    body = {
        "service": "svc-a",
        "signal_type": "metric",
        "source_type": "mock",
        "domain": "infra",
        "source_config": {"url": "http://example.com/metrics"},
    }
    created = client.post("/v1/monitors", json=body)
    assert created.status_code == 201
    target_id = created.json()["target_id"]

    resp = client.delete("/v1/config/infra")
    assert resp.status_code == 400
    assert resp.json()["code"] == "CONFIG_ERROR"
    assert target_id in resp.json()["reason"]
    # 域仍在（未被删）
    assert client.get("/v1/config/infra").status_code == 200


def test_delete_domain_config_removes_from_list(client):
    _put_custom_domain(client, "infra")
    assert client.delete("/v1/config/infra").status_code == 204
    items = client.get("/v1/config").json()["items"]
    assert all(i["domain"] != "infra" for i in items)
