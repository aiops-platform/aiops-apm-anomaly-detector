"""``POST /v1/problems/{id}/escalate``：**直接派单**（不经过诊断裁定）。

页面上的「Escalate」（列表行 / 诊断弹框的「升级 · 修复流程」）走的就是这条：本仓取号、
建单时钉住调用方指定的流程、把问题单转 ``escalated``。四条性质必须钉住：

1. **派单方是本仓**：号由 ``SequenceStore`` 取、绑定写进 evidence（回传才反查得回来）；
2. **evidence 与「升级裁定」同一个形状**（``diagnose_decision`` + ``escalate``），
   但 ``engine=manual`` 且**没有** ``session_id`` —— 它没有诊断轮次；
3. **幂等**：已经持有工单的单再派 → 409，不建第二张；
4. **名字查不到不退回默认**：钉 ``bug-fix-scenario2`` 却跑了别的流程，比跑不起来更坏。
"""

import asyncio

import httpx
import pytest

from aiops_apm.collectors import _gateway
from tests.test_problem_diagnose_api import _seed_log_record  # noqa: E402  -- 复用既有 seed

AGENTFLOW_BASE = "http://localhost:8000"

WORKFLOWS = [
    {"id": "wf_latest", "name": "bug-fix-scenario2", "created_at": "2026-09-17T08:13:09+00:00"},
    {"id": "wf_older", "name": "bug-fix-scenario2", "created_at": "2026-08-01T00:00:00+00:00"},
    {"id": "wf_other", "name": "problem-diagnose-fix", "created_at": "2026-09-22T03:31:30+00:00"},
]


def _resp(status, payload, method="GET", url=AGENTFLOW_BASE + "/workflows"):
    return httpx.Response(status, json=payload, request=httpx.Request(method, url))


class AgentflowStub:
    """出站桩：``GET /workflows`` 回预设表，``POST /tickets`` **回显**收到的号。

    建单那里刻意回显而不是写死一个号：要验的关系是"号由**本仓**取 → 原样送去 → 原样存回"，
    写死的话两边各错各的也照样相等。
    """

    def __init__(self, *, workflows=WORKFLOWS, workflows_status=200):
        self.calls: list[dict] = []
        self.workflows = workflows
        self.workflows_status = workflows_status

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        if url.endswith("/workflows"):
            return _resp(self.workflows_status, self.workflows, method=method, url=url)
        sent = (kwargs.get("json") or {}).get("number")
        return _resp(201, {"id": "t_9f3c11", "number": sent}, method="POST", url=url)

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _allow_loopback(client, monkeypatch):
    # bug_solve_base_url 是 http://localhost:8000（回环）：放行回环并钉死 DNS。
    _gateway.OutboundGateway.set_allow_loopback(True)
    monkeypatch.setattr(_gateway, "_resolve_ips", lambda host: ["127.0.0.1"])
    yield
    _gateway.OutboundGateway.set_allow_loopback(False)


def _wire(client, **kwargs) -> AgentflowStub:
    stub = AgentflowStub(**kwargs)
    client.app.state.http_client = stub
    return stub


def _rec(client, record_id="PR-0001", tenant="default"):
    return asyncio.run(client.app.state.storage.records.get(tenant, record_id))


def test_escalate_pins_workflow_and_binds_ticket(client):
    """钉流程：按**名字**查回 id（重名取最新那条），号由本仓取、绑定落 evidence。"""
    _seed_log_record(client)
    stub = _wire(client)

    resp = client.post(
        "/v1/problems/PR-0001/escalate", json={"workflow_name": "bug-fix-scenario2"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "escalated"
    assert body["ticket_number"].startswith("INC-")
    assert body["workflow_name"] == "bug-fix-scenario2"

    # 出站：先查流程（按名字），再建单（带 workflow_id）
    assert [c["url"] for c in stub.calls] == [
        AGENTFLOW_BASE + "/workflows",
        AGENTFLOW_BASE + "/tickets",
    ]
    sent = stub.calls[1]["json"]
    assert sent["workflow_id"] == "wf_latest", "重名时取 created_at 最新那条"
    assert sent["number"] == body["ticket_number"]
    assert sent["bug_report"]["number"] == "PR-0001"  # 工单入参里的「来源单号」仍是问题单号

    rec = _rec(client)
    assert rec["state"] == "escalated"
    assert rec["resolve_reason"] == f"escalated:{body['ticket_number']}"
    entry = [e for e in rec["evidence"] if e.get("type") == "diagnose_decision"][-1]
    # 与「升级裁定」同一个形状（`_created_ticket` / `_holds_ticket` 都靠它）
    assert entry["decision"] == "escalate"
    assert entry["engine"] == "manual"
    assert entry["ticket_number"] == body["ticket_number"]
    assert entry["workflow_name"] == "bug-fix-scenario2"
    # 没有诊断轮次 → 不写 session_id（前端据此不打印"该轮快照不可用"）
    assert "session_id" not in entry


def test_escalate_without_pin_skips_workflow_lookup(client):
    """不传名字 = 不钉：**不该**白查一次流程表（建单那次是唯一的出站）。"""
    _seed_log_record(client)
    stub = _wire(client)

    resp = client.post("/v1/problems/PR-0001/escalate", json={})
    assert resp.status_code == 200
    assert [c["url"] for c in stub.calls] == [AGENTFLOW_BASE + "/tickets"]
    assert "workflow_id" not in stub.calls[0]["json"]
    assert _rec(client)["state"] == "escalated"


def test_escalate_is_idempotent_guarded(client):
    """已经持有工单的单再派一次 → 409，且**不再出站**（不建第二张）。"""
    _seed_log_record(client)
    _wire(client)
    first = client.post(
        "/v1/problems/PR-0001/escalate", json={"workflow_name": "bug-fix-scenario2"}
    )
    assert first.status_code == 200

    retry = _wire(client)
    resp = client.post("/v1/problems/PR-0001/escalate", json={"workflow_name": "bug-fix-scenario2"})
    assert resp.status_code == 409
    assert "already holds ticket" in resp.json()["reason"]
    assert retry.calls == []


def test_escalate_unknown_workflow_is_404_not_default(client):
    """名字不在库里 → 404 **在**建单之前（绝不退回"不钉"跑一条别的流程）。"""
    _seed_log_record(client)
    stub = _wire(client)

    resp = client.post("/v1/problems/PR-0001/escalate", json={"workflow_name": "nope"})
    assert resp.status_code == 404
    assert "nope" in resp.json()["reason"]
    # 只查了流程表，没建单；问题单状态不变
    assert [c["url"] for c in stub.calls] == [AGENTFLOW_BASE + "/workflows"]
    assert _rec(client)["state"] == "pending"


def test_escalate_agentflow_down_is_upstream_not_404(client):
    """流程表拉不到（agentflow 挂）→ 502，**不是** 404：部署故障不能报成"没有这条流程"。"""
    _seed_log_record(client)
    _wire(client, workflows={}, workflows_status=503)

    resp = client.post(
        "/v1/problems/PR-0001/escalate", json={"workflow_name": "bug-fix-scenario2"}
    )
    assert resp.status_code == 502
    assert _rec(client)["state"] == "pending"


def test_escalate_terminal_state_guarded(client):
    """已 resolved/closed 的单不派单（人已经定过结论，不能替现场翻案）。"""
    _seed_log_record(client)
    _wire(client)
    assert client.post("/v1/problems/PR-0001/ignore").status_code == 200

    resp = client.post(
        "/v1/problems/PR-0001/escalate", json={"workflow_name": "bug-fix-scenario2"}
    )
    assert resp.status_code == 409
    assert "escalate not allowed" in resp.json()["reason"]


def test_ticket_callback_finds_directly_escalated_record(client):
    """闭环：直接派单写下的号，回传时**反查得到**这条记录（这正是"必须经本仓建单"的理由）。"""
    _seed_log_record(client)
    _wire(client)
    number = client.post("/v1/problems/PR-0001/escalate", json={}).json()["ticket_number"]

    resp = client.post(
        "/v1/problems/ticket-status",
        json={"ticket_id": number, "status": "resolved", "description": "已修复"},
    )
    assert resp.status_code == 200
    assert resp.json()["record_id"] == "PR-0001"
    assert resp.json()["state_changed"] is True
    assert _rec(client)["state"] == "resolved"
