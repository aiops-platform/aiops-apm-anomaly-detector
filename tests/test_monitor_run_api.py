"""UC-6.2：``POST /v1/monitors/{id}/run`` 手动单跑。"""

import asyncio


def _seed_target(client):
    body = {
        "service": "svc-a",
        "signal_type": "metric",
        "source_type": "mock",
        "domain": "application",
        "source_config": {},
        "schedule": {"interval_sec": 60},
        "enabled": True,
    }
    asyncio.run(client.app.state.storage.monitor_targets.create("default", body))


def test_run_monitor_returns_domain_result(client):
    _seed_target(client)
    resp = client.post("/v1/monitors/MT-0001/run")
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] == "application"
    assert data["records"] == []  # mock 无信号 → 不开单
    assert data["suppressed_count"] == 0
    assert data["anomaly_count"] == 0
    assert data["degraded_sources"] == []
    assert isinstance(data["timeline"], list)
    assert data["timeline"][0]["step"] == "collect_done"


def test_run_monitor_unknown_target_404(client):
    resp = client.post("/v1/monitors/MT-9999/run")
    assert resp.status_code == 404
