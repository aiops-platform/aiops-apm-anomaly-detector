"""UC-6.3：``POST /v1/alerts/run`` 手动全跑汇总。"""

import asyncio


def _seed_target(client, *, service="svc-a", domain="application"):
    body = {
        "service": service,
        "signal_type": "metric",
        "source_type": "mock",
        "domain": domain,
        "source_config": {},
        "schedule": {"interval_sec": 60},
        "enabled": True,
    }
    asyncio.run(client.app.state.storage.monitor_targets.create("default", body))


def test_alerts_run_returns_summary(client):
    _seed_target(client, service="svc-a", domain="application")
    _seed_target(client, service="svc-b", domain="application")
    resp = client.post("/v1/alerts/run")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["rounds"]) == 1  # 同一 (tenant, domain) 一组
    r = data["rounds"][0]
    assert r["domain"] == "application"
    assert r["target_count"] == 2
    assert r["degraded_sources"] == []
    assert data["total_records"] == 0  # mock 无信号 → 不开单


def test_alerts_run_domain_filter(client):
    _seed_target(client, service="svc-a", domain="application")
    resp = client.post("/v1/alerts/run", params={"domain": "infra"})
    assert resp.status_code == 200
    assert resp.json() == {"rounds": [], "total_records": 0}
