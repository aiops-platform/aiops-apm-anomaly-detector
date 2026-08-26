"""UC-7.2 ``/v1/audit``：轮次列表 + 被抑制信号摊平。"""

import asyncio
from datetime import datetime, timezone

TS1 = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
TS2 = datetime(2026, 8, 26, 12, 1, 0, tzinfo=timezone.utc)

_SUPPRESSED_TIMELINE = [
    {"step": "collect_done", "ts": TS1, "count": 3},
    {
        "step": "suppressed",
        "ts": TS1,
        "count": 2,
        "details": [
            {"signal": "metric:heap_usage", "service": "svc-a", "suppressor": "maintenance_window", "reason": "mw"},
            {"signal": "metric:cpu_usage", "service": "svc-b", "suppressor": "blacklist", "reason": "bl"},
        ],
    },
    {"step": "detected", "count": 1},
]


def _seed_rounds(client) -> None:
    store = client.app.state.storage.rounds
    asyncio.run(store.create_round("default", "R-0001", "application", started_at=TS1, target_ids=["MT-0001"]))
    asyncio.run(store.update_status("default", "R-0001", "success", ended_at=TS2, timeline=_SUPPRESSED_TIMELINE))
    asyncio.run(store.create_round("default", "R-0002", "orders", started_at=TS2, target_ids=["MT-0002"]))
    # 另一租户的轮次（隔离验证）
    asyncio.run(store.create_round("tenant-b", "R-0009", "application", started_at=TS1))


def test_list_rounds_default(client):
    _seed_rounds(client)
    resp = client.get("/v1/audit/rounds")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    # started_at 倒序：R-0002 最新在前
    assert items[0]["round_id"] == "R-0002"
    assert items[1]["round_id"] == "R-0001"
    assert items[1]["status"] == "success"
    assert items[1]["timeline"][0]["step"] == "collect_done"


def test_list_rounds_filters(client):
    _seed_rounds(client)
    assert len(client.get("/v1/audit/rounds", params={"domain": "application"}).json()["items"]) == 1
    assert len(client.get("/v1/audit/rounds", params={"status": "success"}).json()["items"]) == 1
    assert len(client.get("/v1/audit/rounds", params={"limit": 1}).json()["items"]) == 1
    assert client.get("/v1/audit/rounds", params={"domain": "orders"}).json()["items"][0]["round_id"] == "R-0002"


def test_list_rounds_tenant_isolated(client):
    _seed_rounds(client)
    assert len(client.get("/v1/audit/rounds", headers={"X-Tenant-Id": "tenant-b"}).json()["items"]) == 1
    assert len(client.get("/v1/audit/rounds").json()["items"]) == 2


def test_list_suppressed_flattens_timeline(client):
    _seed_rounds(client)
    resp = client.get("/v1/audit/suppressed")
    assert resp.status_code == 200
    rows = resp.json()["items"]
    assert len(rows) == 2
    assert all(r["round_id"] == "R-0001" for r in rows)
    assert rows[0]["signal"].startswith("metric:")
    assert rows[0]["service"] == "svc-a"
    assert rows[0]["suppressor"] == "maintenance_window"
    assert rows[1]["service"] == "svc-b"
    assert rows[1]["suppressor"] == "blacklist"


def test_list_suppressed_service_filter(client):
    _seed_rounds(client)
    rows = client.get("/v1/audit/suppressed", params={"service": "svc-a"}).json()["items"]
    assert len(rows) == 1
    assert rows[0]["service"] == "svc-a"
