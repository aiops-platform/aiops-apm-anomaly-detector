"""``POST /v1/problems/{record_id}/analyze`` → 组平铺 ticket + agentflow /run + in_progress 翻转。"""

import asyncio
import logging
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
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": kwargs.get("json"),
                "headers": kwargs.get("headers"),
                "timeout": kwargs.get("timeout"),
            }
        )
        return self._resp

    async def aclose(self) -> None:
        # app 生命周期 teardown 会调用 http_client.aclose()，桩需兼容
        return None


class TimeoutHttp:
    """出站恒抛读超时。

    **刻意用空消息**：真实场景里 ``httpx.ReadTimeout`` 的 ``str()`` 就是空的
    （2026-09-23 实测 ``reason`` 落在 ``"agent workflow run start failed: "``），
    所以桩必须复现这个形状，否则测不出"靠空字符串根本认不出是超时"。
    """

    def __init__(self):
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "timeout": kwargs.get("timeout")})
        raise httpx.ReadTimeout("", request=httpx.Request(method, url))

    async def aclose(self) -> None:
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
    assert set(body.keys()) == {"workflow_id", "ticket", "tenant_id"}
    assert body["workflow_id"] == "wf-1"
    # 租户桥接：未配 agentflow_tenant → 原样转发请求租户（单租户部署行为不变）。
    # **头与 body 都要给**：agentflow 的 POST /run 在 dev 模式按 body.tenant_id 决定 run 落哪个库，
    # 头只影响 workflow/配置的查找；只给头会"半程换租户"（run 掉进 local，且那里没有 MCP 绑定）。
    assert call["headers"]["X-Tenant-ID"] == "default"
    assert body["tenant_id"] == "default"

    # ⚠️ inputs 必须**包一层 bug_report**：workflow 里各 agent 的入参是
    # $.inputs.bug_report[.cmdb_ci.name]。早期发平铺 ticket 时该路径恒为 None，
    # 工作流在 triage.require 处就失败。时间窗同批下发（日志查询用）。
    inputs = body["ticket"]
    assert set(inputs.keys()) == {"bug_report", "window_start", "window_end"}
    # detected_at = 12:00Z，无 first/last_seen → 12:00±10min = 20min，过窄撑到 30min
    assert inputs["window_end"] == "2026-08-26T12:10:00+00:00"
    assert inputs["window_start"] == "2026-08-26T11:40:00+00:00"

    ticket = inputs["bug_report"]
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
    """工作流入参契约：``bug_report.requestId`` = log_trace_ids.trace_ids[0]（链路 ID）。"""
    _seed_with_log_traces(client)
    capture = CapturingHttp(_ok_resp())
    client.app.state.http_client = capture
    resp = client.post("/v1/problems/PR-0002/analyze", json={"workflow_id": "wf-1"})
    assert resp.status_code == 200
    ticket = capture.calls[0]["json"]["ticket"]["bug_report"]
    assert ticket["requestId"] == "2430a48a7e4d4a4f97b2788ed6a8891b"


def test_analyze_outbound_tenant_bridged(client):
    """配了 agentflow_tenant → 出站带它，而不是请求租户（两侧租户不同，必须显式桥接）。"""
    _seed(client)
    client.app.state.settings.agentflow_tenant = "otr"
    capture = CapturingHttp(_ok_resp())
    client.app.state.http_client = capture
    assert client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": "wf-1"}).status_code == 200
    assert capture.calls[0]["headers"]["X-Tenant-ID"] == "otr"
    assert capture.calls[0]["json"]["tenant_id"] == "otr"


def test_analyze_rerun_appends_second_binding_without_state_flip(client):
    """打回后重跑：in_progress + rerun=true → 起新 run 并**追加**绑定（读取取最后一条）。"""
    _seed(client)
    client.app.state.http_client = CapturingHttp(_ok_resp(run_id="run_1"))
    assert client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": "wf-1"}).status_code == 200

    capture2 = CapturingHttp(_ok_resp(run_id="run_2"))
    client.app.state.http_client = capture2
    resp = client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": "wf-1", "rerun": True})
    assert resp.status_code == 200
    assert resp.json() == {"record_id": "PR-0001", "state": "in_progress", "run_id": "run_2"}

    detail = client.get("/v1/problems/PR-0001").json()
    assert detail["state"] == "in_progress"
    runs = [e for e in detail["evidence"] if e.get("type") == "agent_run"]
    assert [e["run_id"] for e in runs] == ["run_1", "run_2"]


def test_analyze_without_service_400(client):
    """没有服务名 → 400（取数节点按它过滤，缺了只能靠猜）。"""
    _seed(client, service="")
    client.app.state.http_client = CapturingHttp(_ok_resp())
    resp = client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": "wf-1"})
    assert resp.status_code == 400
    assert "service" in resp.json()["reason"]


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


def test_analyze_uses_dedicated_run_start_timeout(client):
    """起 run 用**专用**超时，不共用采集器的 ``outbound_timeout_sec``。

    2026-09-23 实测：agentflow 的 ``POST /run`` 在返回前同步准备工作区（拉修复侧仓库），
    耗时超过共用的 10s → 稳定超时。两个值必须分开，否则放宽其一就会连带影响采集器。
    """
    _seed(client)
    http = CapturingHttp(_ok_resp())
    client.app.state.http_client = http

    assert client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": "wf-1"}).status_code == 200

    settings = client.app.state.settings
    assert http.calls[0]["timeout"] == settings.run_start_timeout_sec
    assert settings.run_start_timeout_sec > settings.outbound_timeout_sec


def test_analyze_run_start_timeout_is_distinguishable_and_warns_against_retry(client):
    """超时必须是**可识别**的文案，且点明"run 可能已在后台跑、重试会再起一个"。

    为什么这两点都要守：
    - **可识别**：``httpx.ReadTimeout`` 的 ``str()`` 是空的，走通用分支只会得到
      ``"agent workflow run start failed: "`` —— 实测就是凭这个空字符串去反推原因，推错了方向。
    - **拦住重试**：超时**不阻止** agentflow 把 run 跑起来（实测：两次"失败"的 analyze
      各留下一个真 run）。措辞若只说失败，用户重试 → 每次多一个孤儿 run，还占并发配额。
    """
    _seed(client)
    client.app.state.http_client = TimeoutHttp()

    resp = client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": "wf-1"})

    assert resp.status_code == 502
    reason = resp.json()["reason"]
    assert "timed out" in reason
    assert "MAY ALREADY BE RUNNING" in reason
    assert "ANOTHER run" in reason
    # 超时同样不写绑定（拿不到 run_id，写不了）
    detail = client.get("/v1/problems/PR-0001").json()
    assert not [e for e in detail["evidence"] if e.get("type") == "agent_run"]


def test_upstream_failure_is_logged(client, caplog):
    """上游失败必须留日志 —— 否则访问日志里只剩一个光秃秃的状态码。

    这是本次排查的实际教训：502 的 ``reason`` 只存在于响应体（前端一闪而过的 toast），
    库里、访问日志里都没有，最后只能凭"reason 是空字符串"去反推，推错了方向。
    """
    _seed(client)
    client.app.state.http_client = TimeoutHttp()

    with caplog.at_level(logging.WARNING, logger="aiops_apm._app"):
        resp = client.post("/v1/problems/PR-0001/analyze", json={"workflow_id": "wf-1"})

    assert resp.status_code == 502
    logged = [r.getMessage() for r in caplog.records]
    assert any("/v1/problems/PR-0001/analyze" in m for m in logged), logged
    assert any("timed out" in m for m in logged), logged
