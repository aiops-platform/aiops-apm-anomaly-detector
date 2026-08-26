"""UC-6.8：多租户鉴权（api_keys 配置了才强制；跨租户 403；admin 路由校验）。"""

from contextlib import contextmanager

from fastapi.testclient import TestClient

from aiops_apm._app import create_app
from aiops_apm.settings import Settings

API_KEYS = {"k1": "tenant-a", "k2": "*"}  # k1 限 tenant-a；k2 master key（admin）


@contextmanager
def _app(api_keys: dict):
    app = create_app(
        Settings(_env_file=None, storage_backend="memory", enable_scheduler=False, api_keys=api_keys)
    )
    with TestClient(app) as c:
        yield c


# ---- 未配置 → 放行（既有行为） ----


def test_unconfigured_is_permissive(client):
    assert client.get("/v1/monitors").status_code == 200


# ---- 配置了才强制 ----


def test_missing_key_401():
    with _app(API_KEYS) as c:
        assert c.get("/v1/monitors").status_code == 401


def test_bad_key_401():
    with _app(API_KEYS) as c:
        assert c.get("/v1/monitors", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_scoped_key_allows_own_tenant():
    with _app(API_KEYS) as c:
        resp = c.get("/v1/monitors", headers={"Authorization": "Bearer k1", "X-Tenant-Id": "tenant-a"})
        assert resp.status_code == 200


def test_cross_tenant_403():
    with _app(API_KEYS) as c:
        resp = c.get("/v1/monitors", headers={"Authorization": "Bearer k1", "X-Tenant-Id": "tenant-b"})
        assert resp.status_code == 403
        assert resp.json()["code"] == "PERMISSION_DENIED"


def test_wildcard_key_allows_any_tenant():
    with _app(API_KEYS) as c:
        assert c.get("/v1/monitors", headers={"Authorization": "Bearer k2", "X-Tenant-Id": "tenant-z"}).status_code == 200


def test_default_tenant_is_default():
    with _app(API_KEYS) as c:
        # 未带 X-Tenant-Id → default；k1 scope 只含 tenant-a → 403
        assert c.get("/v1/monitors", headers={"Authorization": "Bearer k1"}).status_code == 403


# ---- admin 路由校验 ----


def test_alerts_run_requires_admin():
    with _app(API_KEYS) as c:
        assert c.post("/v1/alerts/run", headers={"Authorization": "Bearer k1"}).status_code == 403
        assert c.post("/v1/alerts/run", headers={"Authorization": "Bearer k2"}).status_code == 200


def test_plugins_reload_requires_admin():
    with _app(API_KEYS) as c:
        assert c.post("/v1/plugins/reload", headers={"Authorization": "Bearer k1"}).status_code == 403
        assert c.post("/v1/plugins/reload", headers={"Authorization": "Bearer k2"}).status_code == 200


def test_config_put_requires_admin():
    with _app(API_KEYS) as c:
        body = {"detectors": []}
        assert c.put("/v1/config/application", json=body, headers={"Authorization": "Bearer k1"}).status_code == 403
        assert c.put("/v1/config/application", json=body, headers={"Authorization": "Bearer k2"}).status_code == 200
