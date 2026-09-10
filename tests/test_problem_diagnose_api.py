"""``POST/GET /v1/problems/{record_id}/diagnose`` → 拼装 /diagnose/logs + session_id 绑定。

「分析new」路径：不翻 state，只在 evidence 追加 ``diagnose_session``；同源读诊断服务 /status。
"""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from aiops_apm.collectors import _gateway
from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly
from aiops_apm.models.record import Correlation, ProblemRecord, Verification

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
DIAGNOSE_BASE = "http://localhost:8017"
BIZ_TRACE = "2430a48a7e4d4a4f97b2788ed6a8891b"


def _verification(severity="high"):
    return Verification(passed=True, persistence_ok=True, final_severity=severity)


def _seed_log_record(client, *, record_id="PR-0001", service="sip-aiops-management"):
    """带业务 trace_ids 的日志异常记录 + emit 预置的 ``log_trace_ids`` evidence。"""
    log_anom = LogAnomaly(
        service=service, level="ERROR",
        signature='Cannot invoke "String.trim()" because the return value ... getAssignee() is null x7',
        pattern="NullPointerException", count=7, first_seen=TS, severity="high",
        trace_ids=[BIZ_TRACE, "1c9b0f5a2d8e4f6a9b3c7d2e1f0a8b4c"],
    )
    rec = ProblemRecord(
        record_id=record_id,
        domain="application",
        service=service,
        severity="high",
        detected_at=TS,
        symptom={"summary": f"{service} log ERROR"},
        metric_anomalies=[],
        log_anomalies=[log_anom],
        correlation=Correlation(related=False, reason="log_only"),
        verification=_verification(),
    )
    rec.evidence = [
        {
            "type": "log_trace_ids",
            "round_id": "trace-round-1",
            "target_ids": [],
            "trace_ids": [BIZ_TRACE],
            "count": 1,
        }
    ]
    asyncio.run(client.app.state.storage.records.write_or_append("default", rec))


def _seed_metric_only(client, *, record_id="PR-0002", service="svc-m", summary=""):
    """纯指标、无日志异常（可留空 summary）→ 拼不出 log_excerpt。"""
    anomaly = MetricAnomaly(
        service=service, metric="cpu_usage", value=0.95, method="static_threshold",
        severity="high", detected_at=TS,
    )
    rec = ProblemRecord(
        record_id=record_id,
        domain="application",
        service=service,
        severity="high",
        detected_at=TS,
        symptom={"summary": summary},
        metric_anomalies=[anomaly],
        log_anomalies=[],
        correlation=Correlation(related=False, reason="metric_only"),
        verification=_verification(),
    )
    asyncio.run(client.app.state.storage.records.write_or_append("default", rec))


class CapturingHttp:
    """替换 app.state.http_client 的桩：记录出站调用并返回预设响应。

    传入单个响应 → 每次调用都返回它；传入响应**序列**（list/tuple）→ 按序返回，用尽后沿用最后一个
    （覆盖 reject 的两次出站：先 /remediate 再 /approve）。
    """

    def __init__(self, resp):
        self.calls = []
        if isinstance(resp, (list, tuple)):
            self._queue = list(resp)
            self._resp = self._queue[0] if self._queue else None
        else:
            self._queue = None
            self._resp = resp

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        if self._queue:
            self._resp = self._queue.pop(0)
        return self._resp

    async def aclose(self) -> None:
        return None


def _resp(status, payload, method="POST", url=DIAGNOSE_BASE + "/diagnose/logs"):
    return httpx.Response(status, json=payload, request=httpx.Request(method, url))


def _ok_resp(session_id="sess_abc"):
    return _resp(200, {"session_id": session_id, "status": "analyzing"})


@pytest.fixture(autouse=True)
def _allow_loopback_for_diagnose(client, monkeypatch):
    # diagnose_base_url 是 http://localhost:8017（回环）：单测需放行回环并钉死 DNS。
    _gateway.OutboundGateway.set_allow_loopback(True)
    monkeypatch.setattr(_gateway, "_resolve_ips", lambda host: ["127.0.0.1"])
    yield
    _gateway.OutboundGateway.set_allow_loopback(False)


def test_diagnose_assembles_body_and_binds_session(client):
    _seed_log_record(client)
    capture = CapturingHttp(_ok_resp())
    client.app.state.http_client = capture

    resp = client.post("/v1/problems/PR-0001/diagnose", json={})  # 前端发的空体
    assert resp.status_code == 200
    assert resp.json() == {"record_id": "PR-0001", "session_id": "sess_abc", "status": "analyzing"}

    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == DIAGNOSE_BASE + "/diagnose/logs"
    payload = call["json"]
    assert payload["app"] == "sip-aiops-management"
    assert payload["repo"] == "sip-aiops-management"  # 未配 diagnose_repo → 回退 service
    assert payload["trace_id"] == BIZ_TRACE            # 业务链路 ID（非 round id）
    assert payload["log_excerpt"].startswith('Cannot invoke "String.trim()"')

    detail = client.get("/v1/problems/PR-0001").json()
    assert detail["state"] == "pending"  # 分析new 不翻 state
    bound = [e for e in detail["evidence"] if e.get("type") == "diagnose_session"]
    assert len(bound) == 1
    assert bound[0]["session_id"] == "sess_abc"
    assert bound[0]["trace_id"] == BIZ_TRACE


def test_diagnose_repo_settings_default_and_body_override(client):
    _seed_log_record(client)
    client.app.state.settings.diagnose_repo = "repo-from-config"

    capture = CapturingHttp(_ok_resp(session_id="s1"))
    client.app.state.http_client = capture
    assert client.post("/v1/problems/PR-0001/diagnose").status_code == 200
    assert capture.calls[0]["json"]["repo"] == "repo-from-config"

    # 已有绑定 → 409，需换一条记录；直接验证 body 覆盖优先级
    _seed_log_record(client, record_id="PR-0003", service="svc-x")
    capture2 = CapturingHttp(_ok_resp(session_id="s2"))
    client.app.state.http_client = capture2
    assert client.post("/v1/problems/PR-0003/diagnose", json={"repo": "explicit"}).status_code == 200
    assert capture2.calls[0]["json"]["repo"] == "explicit"


def test_diagnose_already_bound_409_no_second_call(client):
    _seed_log_record(client)
    client.app.state.http_client = CapturingHttp(_ok_resp())
    assert client.post("/v1/problems/PR-0001/diagnose").status_code == 200

    capture2 = CapturingHttp(_ok_resp(session_id="sess_def"))
    client.app.state.http_client = capture2
    resp = client.post("/v1/problems/PR-0001/diagnose")
    assert resp.status_code == 409
    assert resp.json()["code"] == "STATE_CONFLICT"
    assert capture2.calls == []


def test_diagnose_missing_record_404(client):
    capture = CapturingHttp(_ok_resp())
    client.app.state.http_client = capture
    resp = client.post("/v1/problems/PR-9999/diagnose")
    assert resp.status_code == 404
    assert capture.calls == []


def test_diagnose_no_log_excerpt_400(client):
    _seed_metric_only(client)
    capture = CapturingHttp(_ok_resp())
    client.app.state.http_client = capture
    resp = client.post("/v1/problems/PR-0002/diagnose")
    assert resp.status_code == 400
    assert capture.calls == []


def test_diagnose_resolved_409(client):
    _seed_log_record(client)
    client.post("/v1/problems/PR-0001/resolve")
    capture = CapturingHttp(_ok_resp())
    client.app.state.http_client = capture
    resp = client.post("/v1/problems/PR-0001/diagnose")
    assert resp.status_code == 409
    assert capture.calls == []


def test_diagnose_upstream_failure_502_no_binding(client):
    _seed_log_record(client)
    client.app.state.http_client = CapturingHttp(
        _resp(500, {"detail": "boom"})
    )
    resp = client.post("/v1/problems/PR-0001/diagnose")
    assert resp.status_code == 502
    assert resp.json()["code"] == "UPSTREAM_ERROR"
    detail = client.get("/v1/problems/PR-0001").json()
    assert not [e for e in detail["evidence"] if e.get("type") == "diagnose_session"]


def test_get_diagnosis_proxies_spike_status(client):
    _seed_log_record(client)
    client.app.state.http_client = CapturingHttp(_ok_resp(session_id="sess_abc"))
    assert client.post("/v1/problems/PR-0001/diagnose").status_code == 200

    snapshot = {
        "session_id": "sess_abc",
        "status": "completed",
        "trigger": "log",
        "conclusion": {"root_cause": "NPE on assignee", "confidence": "high"},
    }
    capture = CapturingHttp(
        _resp(200, snapshot, method="GET", url=DIAGNOSE_BASE + "/status/sess_abc")
    )
    client.app.state.http_client = capture
    resp = client.get("/v1/problems/PR-0001/diagnose")
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert resp.json()["conclusion"]["root_cause"] == "NPE on assignee"
    assert capture.calls[0]["url"] == DIAGNOSE_BASE + "/status/sess_abc"
    assert capture.calls[0]["method"] == "GET"


def test_get_diagnosis_unbound_404(client):
    _seed_log_record(client)
    resp = client.get("/v1/problems/PR-0001/diagnose")
    assert resp.status_code == 404


def test_get_diagnosis_upstream_failure_502(client):
    _seed_log_record(client)
    client.app.state.http_client = CapturingHttp(_ok_resp(session_id="sess_abc"))
    assert client.post("/v1/problems/PR-0001/diagnose").status_code == 200

    client.app.state.http_client = CapturingHttp(_resp(404, {"detail": "expired"}, method="GET"))
    resp = client.get("/v1/problems/PR-0001/diagnose")
    assert resp.status_code == 502
    assert resp.json()["code"] == "UPSTREAM_ERROR"


def test_diagnose_after_ignore_is_409(client):
    """忽略关单（state=closed）后终态守卫生效：不可再发起诊断。"""
    _seed_log_record(client)
    client.app.state.http_client = CapturingHttp(_ok_resp(session_id="sess_abc"))
    assert client.post("/v1/problems/PR-0001/diagnose").status_code == 200

    client.app.state.http_client = CapturingHttp(_resp(200, {"status": "dismissed"}))
    assert client.post(
        "/v1/problems/PR-0001/diagnose/decision", json={"decision": "ignore"}
    ).status_code == 200
    assert client.get("/v1/problems/PR-0001").json()["state"] == "closed"

    capture = CapturingHttp(_ok_resp(session_id="sess_new"))
    client.app.state.http_client = capture
    resp = client.post("/v1/problems/PR-0001/diagnose")
    assert resp.status_code == 409
    assert capture.calls == []
