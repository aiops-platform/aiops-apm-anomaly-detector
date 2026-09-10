"""``POST /v1/problems/{record_id}/analyze`` → 组平铺 ticket + agentflow /run + in_progress 翻转。"""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from aiops_apm.collectors import _gateway
from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly
from aiops_apm.models.record import Correlation, ProblemRecord, Verification

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
BASE = "http://localhost:8000"


def _seed(client, *, record_id="PR-0001", service="svc-a", severity="high"):
    anomaly = MetricAnomaly(
        service=service, metric="cpu_usage", value=0.95, method="static_threshold",
        severity=severity, detected_at=TS,
    )
    rec = ProblemRecord(
        record_id=record_id,
        domain="application",
        service=service,
        severity=severity,
        detected_at=TS,
        symptom={"summary": f"{service} cpu_usage 0.95"},
        metric_anomalies=[anomaly],
        log_anomalies=[],
        correlation=Correlation(related=False, reason="metric_only"),
        verification=Verification(passed=True, persistence_ok=True, final_severity=severity),
    )
    asyncio.run(client.app.state.storage.records.write_or_append("default", rec))


class CapturingHttp:
    """替换 app.state.http_client 的桩：记录出站调用并返回预设响应。"""

    def __init__(self, resp):
        self.calls = []
        self._resp = resp

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        return self._resp

    async def aclose(self) -> None:
        # app 生命周期 teardown 会调用 http_client.aclose()，桩需兼容
        return None


def _resp(status, payload):
    return httpx.Response(status, json=payload, request=httpx.Request("POST", BASE + "/run"))


def _ok_resp(run_id="run_abc"):
    return _resp(200, {"run_id": run_id, "status": "started"})


@pytest.fixture(autouse=True)
def _allow_loopback_for_agentflow(client, monkeypatch):
    # base 是 http://localhost:8000（回环）：单测需放行回环并钉死 DNS，避免真实解析 / fail-closed 拦截。
    _gateway.OutboundGateway.set_allow_loopback(True)
    monkeypatch.setattr(_gateway, "_resolve_ips", lambda host: ["127.0.0.1"])
    yield
    _gateway.OutboundGateway.set_allow_loopback(False)


def test_analyze_happy_path_captures_run_and_flips_state(client):
    _seed(client)
    capture = CapturingHttp(_ok_resp())
    client.app.state.http_client = capture

    resp = client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": "wf-1"})
    assert resp.status_code == 200
    assert resp.json() == {"record_id": "PR-0001", "state": "in_progress", "run_id": "run_abc"}

    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == BASE + "/run"
    body = call["json"]
    assert set(body.keys()) == {"workflow_id", "ticket"}
    assert body["workflow_id"] == "wf-1"
    ticket = body["ticket"]
    assert ticket["number"] == "PR-0001"
    assert ticket["state"] == "New"
    assert ticket["impact"] == "2"          # high
    assert ticket["urgency"] == "2"
    assert ticket["priority"] == "2"
    assert ticket["cmdb_ci"]["name"] == "svc-a"
    assert ticket["cmdb_ci"]["service"] == "application"
    assert "metric cpu_usage=0.95" in ticket["description"]
    assert "requestId" not in ticket  # 纯 metric、无 log trace evidence → 不注入链路 ID

    detail = client.get("/v1/problems/PR-0001").json()
    assert detail["state"] == "in_progress"
    agent_run = [e for e in detail["evidence"] if e.get("type") == "agent_run"]
    assert len(agent_run) == 1
    assert agent_run[0]["run_id"] == "run_abc"
    assert agent_run[0]["workflow_id"] == "wf-1"


def _seed_with_log_traces(client, *, record_id="PR-0002", service="svc-b", severity="high"):
    """构造 log 异常带业务 trace_ids 的记录，并预置 emit 产出的 ``log_trace_ids`` evidence。"""
    trace_ids = ["2430a48a7e4d4a4f97b2788ed6a8891b", "1c9b0f5a2d8e4f6a9b3c7d2e1f0a8b4c"]
    log_anom = LogAnomaly(
        service=service, level="ERROR", signature="java.io.IOException", pattern="IOException",
        count=3, first_seen=TS, severity=severity, trace_ids=trace_ids,
    )
    rec = ProblemRecord(
        record_id=record_id,
        domain="application",
        service=service,
        severity=severity,
        detected_at=TS,
        symptom={"summary": f"{service} log ERROR"},
        metric_anomalies=[],
        log_anomalies=[log_anom],
        correlation=Correlation(related=False, reason="log_only"),
        verification=Verification(passed=True, persistence_ok=True, final_severity=severity),
    )
    rec.evidence = [
        {
            "type": "log_trace_ids",
            "round_id": "trace-round-1",
            "target_ids": [],
            "trace_ids": trace_ids,
            "count": len(trace_ids),
        }
    ]
    asyncio.run(client.app.state.storage.records.write_or_append("default", rec))


def test_analyze_ticket_exposes_request_id_from_log_trace_ids(client):
    """git-search 工作流入参契约：ticket 顶层 requestId = log_trace_ids.trace_ids[0]。"""
    _seed_with_log_traces(client)
    capture = CapturingHttp(_ok_resp())
    client.app.state.http_client = capture
    resp = client.post("/v1/problems/PR-0002/analyze", json={"workflow_id": "wf-1"})
    assert resp.status_code == 200
    ticket = capture.calls[0]["json"]["ticket"]
    assert ticket["requestId"] == "2430a48a7e4d4a4f97b2788ed6a8891b"


def test_analyze_missing_record_404(client):
    capture = CapturingHttp(_ok_resp())
    client.app.state.http_client = capture
    resp = client.post("/v1/problems/PR-9999/analyze", json={"workflow_id": "wf-1"})
    assert resp.status_code == 404
    assert capture.calls == []


def test_analyze_empty_workflow_400(client):
    _seed(client)
    resp = client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": ""})
    assert resp.status_code == 400


def test_analyze_already_in_progress_409_no_second_run(client):
    _seed(client)
    client.app.state.http_client = CapturingHttp(_ok_resp())
    assert client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": "wf-1"}).status_code == 200

    # 已 in_progress：守卫先于出站，不得再发起第二次 run
    capture2 = CapturingHttp(_ok_resp(run_id="run_def"))
    client.app.state.http_client = capture2
    resp = client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": "wf-1"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "STATE_CONFLICT"
    assert capture2.calls == []


def test_analyze_resolved_409(client):
    _seed(client)
    client.post("/v1/problems/PR-0001/resolve")
    capture = CapturingHttp(_ok_resp())
    client.app.state.http_client = capture
    resp = client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": "wf-1"})
    assert resp.status_code == 409
    assert capture.calls == []


def test_analyze_upstream_failure_leaves_pending(client):
    _seed(client)
    client.app.state.http_client = CapturingHttp(_resp(502, {"detail": "workflow not found"}))
    resp = client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": "wf-1"})
    assert resp.status_code == 502
    assert resp.json()["code"] == "UPSTREAM_ERROR"
    detail = client.get("/v1/problems/PR-0001").json()
    assert detail["state"] == "pending"
    assert not [e for e in detail["evidence"] if e.get("type") == "agent_run"]
