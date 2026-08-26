"""UC-0.1 系统启动健康检查。"""

from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_ready_not_ready_without_db(client: TestClient) -> None:
    """M0 未构建存储/插件，/ready 应返回 503 + NOT_READY。"""
    res = client.get("/ready")
    assert res.status_code == 503
    body = res.json()
    assert body["code"] == "NOT_READY"
    assert "db" in body["reason"]
