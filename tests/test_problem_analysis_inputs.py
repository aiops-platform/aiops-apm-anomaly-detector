"""问题单 → agentflow ``inputs``：时间窗推导（``_analysis_window``）+ 审批回写端点。

时间窗是 MCP 查询的硬入参（``query_logs`` 必填 start/end，跨度 ≤24h），错了整条诊断就没有意义，
所以规则集中在一处并在此逐条钉死：补齐 → 夹 now → 撑下限 → 截 24h。
"""

import asyncio
from datetime import datetime, timedelta, timezone

from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly
from aiops_apm.models.record import Correlation, ProblemRecord, Verification
from aiops_apm.router.problems import _analysis_window, _build_analysis_inputs

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


def _rec(**kw) -> dict:
    base = {"detected_at": NOW - timedelta(hours=2)}
    base.update(kw)
    return base


def test_window_pads_both_ends():
    """正常区间：first−10min ~ last+10min，跨度够大就不动它。"""
    start, end = _analysis_window(
        _rec(first_seen_at=NOW - timedelta(minutes=60), last_seen_at=NOW - timedelta(minutes=30)),
        now=NOW,
    )
    assert start == (NOW - timedelta(minutes=70)).isoformat()
    assert end == (NOW - timedelta(minutes=20)).isoformat()


def test_window_widened_to_minimum():
    """first == last（单点事件）→ 撑到 30min，否则取不到样本。"""
    start, end = _analysis_window(
        _rec(first_seen_at=NOW - timedelta(minutes=20), last_seen_at=NOW - timedelta(minutes=20)),
        now=NOW,
    )
    assert datetime.fromisoformat(end) - datetime.fromisoformat(start) == timedelta(minutes=30)
    assert end == (NOW - timedelta(minutes=10)).isoformat()


def test_window_end_clamped_to_now():
    """last_seen 落在未来（检测轮晚于最新日志）→ 夹到 now，绝不查未来。"""
    _, end = _analysis_window(
        _rec(first_seen_at=NOW - timedelta(hours=1), last_seen_at=NOW + timedelta(minutes=30)),
        now=NOW,
    )
    assert end == NOW.isoformat()


def test_window_span_clamped_to_24h():
    """跨度 >24h → 截到 24h（MCP 侧 DATASOURCE_MAX_RANGE_HOURS 硬拒绝）。"""
    start, end = _analysis_window(
        _rec(first_seen_at=NOW - timedelta(hours=40), last_seen_at=NOW - timedelta(hours=1)),
        now=NOW,
    )
    assert datetime.fromisoformat(end) - datetime.fromisoformat(start) == timedelta(hours=24)


def test_window_falls_back_to_detected_at():
    """缺 first/last_seen → 用 detected_at，并保证 start < end。"""
    start, end = _analysis_window(_rec(), now=NOW)
    assert datetime.fromisoformat(start) < datetime.fromisoformat(end)
    assert end == (NOW - timedelta(hours=2) + timedelta(minutes=10)).isoformat()
    assert datetime.fromisoformat(end) - datetime.fromisoformat(start) == timedelta(minutes=30)


def test_window_naive_datetime_treated_as_utc():
    """PG ``TIMESTAMP(3)`` 读回是 naive → 按 UTC 解释（与 MCP 的 _parse_window 一致）。"""
    naive = datetime(2026, 9, 17, 9, 0, 0)
    start, end = _analysis_window(_rec(first_seen_at=naive, last_seen_at=naive), now=NOW)
    # 09:00±10min = 20min → 撑到 30min（start 再往前 10min），终点仍是 last+10min
    assert end == (naive + timedelta(minutes=10)).replace(tzinfo=timezone.utc).isoformat()
    assert start == (naive - timedelta(minutes=20)).replace(tzinfo=timezone.utc).isoformat()


def test_window_accepts_iso_strings():
    """记录从 JSON/PG 回来时可能是字符串。"""
    start, end = _analysis_window(
        _rec(first_seen_at="2026-09-17T09:00:00", last_seen_at="2026-09-17T10:00:00"), now=NOW
    )
    assert start.startswith("2026-09-17T08:50:00")
    assert end.startswith("2026-09-17T10:10:00")


def test_build_analysis_inputs_wraps_bug_report():
    """inputs 必须包一层 ``bug_report``（workflow 的 ``$.inputs.bug_report``）+ 时间窗。"""
    rec = ProblemRecord(
        record_id="PR-0001",
        domain="application",
        service="order-service",
        severity="high",
        detected_at=NOW - timedelta(hours=2),
        first_seen_at=NOW - timedelta(hours=2),
        last_seen_at=NOW - timedelta(hours=1),
        symptom={"summary": "结账无响应"},
        metric_anomalies=[],
        log_anomalies=[
            LogAnomaly(
                service="order-service", level="ERROR", signature="java.lang.NullPointerException",
                pattern="NPE", count=13, first_seen=NOW - timedelta(hours=2), severity="high",
            )
        ],
        correlation=Correlation(related=False, reason="log_only"),
        verification=Verification(passed=True, persistence_ok=True, final_severity="high"),
    )
    inputs = _build_analysis_inputs(rec.model_dump())
    assert set(inputs.keys()) == {"bug_report", "window_start", "window_end", "review_feedback"}
    assert inputs["bug_report"]["cmdb_ci"]["name"] == "order-service"
    assert inputs["bug_report"]["short_description"] == "结账无响应"
    # 无驳回 evidence → 空串（**不是缺键**：workflow 侧靠"恒存在"免去区分"缺失 vs 空"两种情况）
    assert inputs["review_feedback"] == ""


def _decided(decision: str, feedback: str = "") -> dict:
    return {"type": "diagnose_decision", "decision": decision, "feedback": feedback}


def test_build_analysis_inputs_carries_latest_reject_feedback():
    """驳回建议必须进 inputs —— 否则 UI 那句"带着这条建议重新分析"是空头支票。

    在此之前 ``feedback`` 只被 ``append_evidence`` 记进 evidence，**从未进入新一轮 run 的输入**：
    重跑的 inputs 与上一轮逐字节相同 → 缺的证据没变 → 大概率再次 halt。
    """
    rec = ProblemRecord(
        record_id="PR-0002",
        domain="application",
        service="order-service",
        severity="high",
        detected_at=NOW - timedelta(hours=2),
        symptom={"summary": "结账无响应"},
        metric_anomalies=[],
        log_anomalies=[],
        correlation=Correlation(related=False, reason="log_only"),
        verification=Verification(passed=True, persistence_ok=True, final_severity="high"),
        evidence=[
            _decided("reject", "第一次：请查 gateway 侧"),
            _decided("ignore"),                      # 非驳回：不带建议，不该被取
            _decided("escalate", "升级时的备注"),      # 同上
            _decided("reject", "  第二次：仓库在 order-service  "),  # 取最新 + 去空白
        ],
    )
    assert _build_analysis_inputs(rec.model_dump())["review_feedback"] == "第二次：仓库在 order-service"


def test_build_analysis_inputs_ignores_blank_reject_feedback():
    """驳回但 feedback 只有空白 → 视为没有，不要把空白串当"有建议"传下去。"""
    rec = ProblemRecord(
        record_id="PR-0003",
        domain="application",
        service="svc-a",
        severity="high",
        detected_at=NOW - timedelta(hours=2),
        symptom={"summary": "x"},
        metric_anomalies=[],
        log_anomalies=[],
        correlation=Correlation(related=False, reason="log_only"),
        verification=Verification(passed=True, persistence_ok=True, final_severity="high"),
        evidence=[_decided("reject", "   ")],
    )
    assert _build_analysis_inputs(rec.model_dump())["review_feedback"] == ""


# ── 审批回写（POST /{id}/run-decision）：只记录，按 (run_id, node_id) 幂等 ──────────


def _seed(client, record_id="PR-DEC") -> None:
    rec = ProblemRecord(
        record_id=record_id,
        domain="application",
        service="svc-a",
        severity="high",
        detected_at=NOW,
        symptom={"summary": "svc-a log ERROR"},
        metric_anomalies=[
            MetricAnomaly(
                service="svc-a", metric="cpu_usage", value=0.95, method="static_threshold",
                severity="high", detected_at=NOW,
            )
        ],
        log_anomalies=[],
        correlation=Correlation(related=False, reason="metric_only"),
        verification=Verification(passed=True, persistence_ok=True, final_severity="high"),
    )
    asyncio.run(client.app.state.storage.records.write_or_append("default", rec))


def test_run_decision_recorded_and_idempotent(client):
    _seed(client)
    body = {"run_id": "run_1", "node_id": "approve-plan", "approved": True, "by": "lead", "comment": "ok"}
    first = client.post("/v1/problems/PR-DEC/run-decision", json=body)
    assert first.status_code == 200
    assert first.json()["recorded"] is True

    # 重试 / 重复点击：不写第二条，返回 duplicate
    second = client.post("/v1/problems/PR-DEC/run-decision", json=body)
    assert second.status_code == 200
    assert second.json() == {**second.json(), "recorded": False, "duplicate": True}

    detail = client.get("/v1/problems/PR-DEC").json()
    decisions = [e for e in detail["evidence"] if e.get("type") == "run_decision"]
    assert len(decisions) == 1
    assert decisions[0]["approved"] is True and decisions[0]["by"] == "lead"

    # 不同 node / 不同 run → 各记一条
    client.post("/v1/problems/PR-DEC/run-decision", json={**body, "run_id": "run_2"})
    detail = client.get("/v1/problems/PR-DEC").json()
    assert len([e for e in detail["evidence"] if e.get("type") == "run_decision"]) == 2


def test_run_decision_missing_record_404(client):
    resp = client.post("/v1/problems/PR-NOPE/run-decision", json={"run_id": "run_1", "approved": False})
    assert resp.status_code == 404


def test_run_decision_requires_run_id(client):
    _seed(client, record_id="PR-DEC2")
    resp = client.post("/v1/problems/PR-DEC2/run-decision", json={"run_id": "", "approved": False})
    assert resp.status_code == 400
