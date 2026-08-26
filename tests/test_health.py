"""UC-0.1 系统启动健康检查。"""

from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_ready_not_ready_without_plugins(client: TestClient) -> None:
    """M2 memory backend：db 就绪，但插件未构建，仍应返回 503 + NOT_READY。"""
    res = client.get("/ready")
    assert res.status_code == 503
    body = res.json()
    assert body["code"] == "NOT_READY"
    assert "'db': True" in body["reason"]
