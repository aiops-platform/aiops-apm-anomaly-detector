"""UC-4.1 / UC-4.2：/v1/plugins 列表与重载。

conftest client fixture 用 memory backend，lifespan 已加载 PluginRegistry（M4 接线）。
"""

from fastapi.testclient import TestClient


def test_plugins_list(client: TestClient) -> None:
    res = client.get("/v1/plugins")
    assert res.status_code == 200
    body = res.json()
    assert set(body["collector"]) == {"http_metrics", "http_logs", "mock"}
    assert set(body["detector"]) == {"static_threshold", "simple_compare", "signature_aggregate"}
    assert set(body["suppressor"]) == {"maintenance_window", "blacklist"}


def test_plugins_reload(client: TestClient) -> None:
    res = client.post("/v1/plugins/reload")
    assert res.status_code == 200
    body = res.json()
    assert set(body["collector"]) == {"http_metrics", "http_logs", "mock"}
    assert set(body["detector"]) == {"static_threshold", "simple_compare", "signature_aggregate"}
    assert set(body["suppressor"]) == {"maintenance_window", "blacklist"}
