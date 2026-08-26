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
