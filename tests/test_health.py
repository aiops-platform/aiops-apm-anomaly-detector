"""UC-0.1 系统启动健康检查。"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from aiops_apm._app import _supervise


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


# ---- 后台任务守护（M7 加固）：worker 崩溃退避重启；取消直接传播 ----


async def test_supervise_restarts_crashed_worker() -> None:
    """worker 首跑异常 → _supervise 记录后重启，直到成功返回。"""
    runs = 0

    async def flaky() -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("boom")

    await asyncio.wait_for(_supervise(flaky, "flaky", delay=0.01, max_delay=1.0), timeout=1.0)
    assert runs == 2


async def test_supervise_propagates_cancellation() -> None:
    """正常停止（应用关闭）→ CancelledError 直接传播，不触发重启。"""
    started = asyncio.Event()

    async def worker() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(_supervise(worker, "worker"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
