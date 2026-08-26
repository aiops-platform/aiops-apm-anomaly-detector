"""pytest 共享 fixture。"""

import pytest
from fastapi.testclient import TestClient

from aiops_apm._app import create_app
from aiops_apm.settings import Settings


@pytest.fixture
def client() -> TestClient:
    """构造应用（memory storage backend）并触发 lifespan，返回同步 TestClient。"""
    app = create_app(Settings(_env_file=None, storage_backend="memory"))
    with TestClient(app) as c:
        yield c
