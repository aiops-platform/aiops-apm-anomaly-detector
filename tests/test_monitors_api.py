"""UC-3.1/3.2/3.7/3.8 监控端点 API：``/v1/monitors`` CRUD + 连通性测试。

用 conftest 的 memory backend + TestClient；``/test`` 通过替换
``app.state.http_client`` 为 FakeHttp 隔离真实网络。
"""

import httpx
import pytest

PROM_URL = "https://prometheus.example.com:9090/api/v1/query"


def _metric_body(**overrides):
    body = {
        "service": "order-management",
        "signal_type": "metric",
        "source_type": "prometheus",
        "domain": "application",
        "source_config": {
            "url": PROM_URL,
            "method": "GET",
            "rows_path": "data.result",
            "field_mapping": {
                "metric": "metric.__name__",
                "value": "value[1]",
                "timestamp": "value[0]",
            },
        },
        "schedule": {"interval_sec": 60},
        "enabled": True,
    }
    body.update(overrides)
    return body


class FakeHttp:
    """替换 ``app.state.http_client``：返回 Prometheus 行，兼容 lifespan 的 aclose。"""

    def __init__(self, rows):
        self._rows = rows

    async def request(self, method: str, url: str, **kwargs):
        return httpx.Response(200, json={"data": {"result": self._rows}}, request=httpx.Request(method, url))

    async def aclose(self) -> None:
        pass


def _rows():
    return [
        {"metric": {"__name__": "cpu_usage"}, "value": [1710000000, "0.91"]},
        {"metric": {"__name__": "cpu_usage"}, "value": [1710000060, "0.95"]},
    ]


# ---- UC-3.1 新增监控端点 ----


def test_create_monitor_returns_target_id(client):
    resp = client.post("/v1/monitors", json=_metric_body())
    assert resp.status_code == 201
    assert resp.json() == {"target_id": "MT-0001"}


def test_create_uses_tenant_header(client):
    client.post("/v1/monitors", json=_metric_body(), headers={"X-Tenant-Id": "tenant-b"})
    # 默认租户看不到 tenant-b 的端点
    assert client.get("/v1/monitors").json()["items"] == []
    # tenant-b 自己能看到
    assert len(client.get("/v1/monitors", headers={"X-Tenant-Id": "tenant-b"}).json()["items"]) == 1


def test_get_and_list(client):
    client.post("/v1/monitors", json=_metric_body())
    client.post(
        "/v1/monitors",
        json=_metric_body(service="svc-b", signal_type="log", source_type="elk"),
    )
    items = client.get("/v1/monitors").json()["items"]
    assert len(items) == 2
    assert len(client.get("/v1/monitors", params={"service": "svc-b"}).json()["items"]) == 1
    assert len(client.get("/v1/monitors", params={"signal_type": "log"}).json()["items"]) == 1

    detail = client.get("/v1/monitors/MT-0001")
    assert detail.status_code == 200
    assert detail.json()["target_id"] == "MT-0001"
    assert client.get("/v1/monitors/MT-9999").status_code == 404


def test_update_patches_and_revalidates(client):
    client.post("/v1/monitors", json=_metric_body())
    resp = client.put("/v1/monitors/MT-0001", json={"service": "new-svc"})
    assert resp.status_code == 200
    assert resp.json()["service"] == "new-svc"
    # 变更 source_config 含 SSRF URL → 400
    bad = client.put("/v1/monitors/MT-0001", json={"source_config": {"url": "http://169.254.169.254/"}})
    assert bad.status_code == 400
    assert bad.json()["code"] == "VALIDATION_ERROR"


def test_delete_is_soft(client):
    client.post("/v1/monitors", json=_metric_body())
    assert client.delete("/v1/monitors/MT-0001").status_code == 204
    # 软删：get 仍返回但 enabled=False
    detail = client.get("/v1/monitors/MT-0001")
    assert detail.status_code == 200
    assert detail.json()["enabled"] is False


# ---- UC-3.7 SSRF 拦截 ----


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:9200/_search",
        "http://10.0.0.1/metrics",
        "file:///etc/passwd",
    ],
)
def test_create_rejects_ssrf_or_bad_scheme(client, url):
    resp = client.post("/v1/monitors", json=_metric_body(source_config={"url": url, "field_mapping": {}}))
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"
    assert client.get("/v1/monitors").json()["items"] == []  # 表无新增


# ---- UC-3.8 Secret 引用 ----


def test_create_rejects_plaintext_credential(client):
    sc = _metric_body()["source_config"]
    sc["headers"] = {"Authorization": "Bearer abc123"}
    resp = client.post("/v1/monitors", json=_metric_body(source_config=sc))
    assert resp.status_code == 400
    assert "plaintext credential" in resp.json()["reason"]


def test_create_accepts_secret_reference(client):
    sc = _metric_body()["source_config"]
    sc["headers"] = {"Authorization": "Bearer ${env:ORDER_TOKEN}"}
    resp = client.post("/v1/monitors", json=_metric_body(source_config=sc))
    assert resp.status_code == 201


# ---- UC-3.2 测试采集连通性 ----


def test_test_monitor_success_returns_samples(client):
    client.post("/v1/monitors", json=_metric_body())
    client.app.state.http_client = FakeHttp(_rows())  # 替换真实 http_client
    resp = client.post("/v1/monitors/MT-0001/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["signal_count"] == 2
    assert len(data["signals"]) == 2
    assert data["signals"][0]["metric"] == "cpu_usage"


def test_test_monitor_upstream_error_returns_structured_error(client):
    client.post("/v1/monitors", json=_metric_body())

    class FailingHttp:
        async def request(self, method, url, **kwargs):
            raise httpx.TimeoutException("upstream timeout")

        async def aclose(self) -> None:
            pass

    client.app.state.http_client = FailingHttp()
    resp = client.post("/v1/monitors/MT-0001/test")
    assert resp.status_code == 200  # 上游失败 → 结构化错误而非 5xx
    data = resp.json()
    assert data["status"] == "error"
    assert data["signal_count"] == 0


def test_test_monitor_unknown_target_404(client):
    assert client.post("/v1/monitors/MT-9999/test").status_code == 404
