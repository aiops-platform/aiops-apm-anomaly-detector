"""UC-0.1 系统启动健康检查。"""

from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_ready_ok_with_plugins(client: TestClient) -> None:
    """M4 memory backend：db:True + plugins:True（registry 已接线）→ 200 ready。"""
    res = client.get("/ready")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"db": True, "plugins": True}
