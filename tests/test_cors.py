"""CORS（本计划 §8.1）：``APM_ALLOWED_ORIGINS`` 配置了才挂中间件；默认空=不挂。

用 memory backend + TestClient 验证预检响应头与默认行为；不触网。
"""

from fastapi.testclient import TestClient

from aiops_apm._app import create_app
from aiops_apm.settings import Settings


def _client(allowed_origins: list[str]) -> TestClient:
    app = create_app(
        Settings(_env_file=None, storage_backend="memory", enable_scheduler=False, allowed_origins=allowed_origins)
    )
    return TestClient(app)


def test_preflight_returns_cors_headers_when_configured() -> None:
    with _client(["http://localhost:8080"]) as c:
        resp = c.options(
            "/v1/monitors",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "http://localhost:8080"
        assert "POST" in resp.headers["access-control-allow-methods"]
        assert "X-Tenant-Id" in resp.headers["access-control-allow-headers"]
        assert "Authorization" in resp.headers["access-control-allow-headers"]


def test_simple_get_echoes_allow_origin_when_configured() -> None:
    with _client(["http://localhost:8080"]) as c:
        resp = c.get("/health", headers={"Origin": "http://localhost:8080"})
        assert resp.headers["access-control-allow-origin"] == "http://localhost:8080"


def test_no_cors_headers_by_default() -> None:
    with _client([]) as c:
        resp = c.options(
            "/v1/monitors",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in resp.headers


def test_disallowed_origin_not_echoed() -> None:
    with _client(["http://localhost:8080"]) as c:
        resp = c.options(
            "/v1/monitors",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in resp.headers
