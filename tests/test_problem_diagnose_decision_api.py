"""``POST /v1/problems/{id}/diagnose/decision`` + ``GET .../diagnose/decisions``。

计划级审批：拒绝（→ spike /remediate + /approve，问题单 state 不变）/ 忽略（→ closed）/
误报（→ resolved + FPR 回写）。历史计划落本仓 ``evidence``（``diagnose_decision``）——spike 侧
``remediation`` 单值、重跑即清空，不能承载历史。
"""

import asyncio

import pytest

from aiops_apm.collectors import _gateway
from aiops_apm.metrics import FALSE_POSITIVE_RATE

from tests.test_problem_diagnose_api import (  # noqa: E402  -- 复用既有桩与 seed
    BIZ_TRACE,
    DIAGNOSE_BASE,
    CapturingHttp,
    _ok_resp,
    _resp,
    _seed_log_record,
)


@pytest.fixture(autouse=True)
def _allow_loopback(client, monkeypatch):
    _gateway.OutboundGateway.set_allow_loopback(True)
    monkeypatch.setattr(_gateway, "_resolve_ips", lambda host: ["127.0.0.1"])
    yield
    _gateway.OutboundGateway.set_allow_loopback(False)


def _bind_session(client, *, record_id="PR-0001", session_id="sess_abc") -> None:
    client.app.state.http_client = CapturingHttp(_ok_resp(session_id=session_id))
    assert client.post(f"/v1/problems/{record_id}/diagnose", json={}).status_code == 200


def _remediate_ok(option_index=2, title="重启依赖 Db", steps=None):
    return _resp(
        200,
        {
            "session_id": "sess_abc",
            "remediation_status": "pending_review",
            "option_index": option_index,
            "option_title": title,
            "steps": steps
            if steps is not None
            else [{"action": "restart", "target": "Deployment payment-db", "risk": "medium"}],
        },
        url=DIAGNOSE_BASE + "/remediate/sess_abc",
    )


def _approve_reject_ok(count=1, max_r=3):
    return _resp(
        200,
        {
            "session_id": "sess_abc",
            "session_status": "completed",  # _schedule_run 不同步置 analyzing → 旧值
            "remediation_status": "rejected",
            "reanalyze_count": count,
            "max_reanalyze": max_r,
        },
        url=DIAGNOSE_BASE + "/approve/sess_abc",
    )


def _status_snap(*, session_id="sess_abc", root_cause="NPE on assignee", supporting_text="NullPointerException at a.py:42"):
    """``GET /status/{sid}`` 200：决策**之前**抓的那份整轮快照（根因/方案/分析链路）。"""
    return _resp(
        200,
        {
            "session_id": session_id,
            "status": "completed",
            "trigger": "log",
            "tasks": [{"id": "t1", "title": "查日志", "status": "done"}],
            "tool_calls": [{"name": "git_blame", "args": {"path": "a.py"}, "result": "ok"}],
            "conclusion": {
                "root_cause": root_cause,
                "confidence": "high",
                "summary": "assignee 为空导致 NPE",
                "evidence": [{"source": "app.log", "supporting_text": supporting_text}],
            },
            "remediation_status": "pending_review",
        },
        method="GET",
        url=DIAGNOSE_BASE + f"/status/{session_id}",
    )


def _dismiss_ok(reason="ignored", session_id="sess_abc"):
    return _resp(
        200,
        {"session_id": session_id, "status": "dismissed", "reason": reason},
        url=DIAGNOSE_BASE + f"/dismiss/{session_id}",
    )


def _decisions(client, record_id="PR-0001"):
    return client.get(f"/v1/problems/{record_id}/diagnose/decisions").json()["items"]


# ======================================================================
# reject
# ======================================================================
def test_reject_selects_option_then_approves_and_records_history(client):
    _seed_log_record(client)
    _bind_session(client)

    capture = CapturingHttp([_status_snap(), _remediate_ok(option_index=2), _approve_reject_ok()])
    client.app.state.http_client = capture
    resp = client.post(
        "/v1/problems/PR-0001/diagnose/decision",
        json={"decision": "reject", "feedback": "DB 运维确认正常，请复核配置指向", "option_index": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "reject"
    assert body["session_status"] == "completed"
    assert body["remediation_status"] == "rejected"
    assert body["reanalyze_count"] == 1
    assert body["max_reanalyze"] == 3
    assert body["record_state"] == "pending"  # 拒绝不翻 state

    # 出站顺序：先 GET /status 抓整轮快照（必须在重跑清空 conclusion 之前），
    # 再 POST /remediate（带 option_index），最后 POST /approve（decision=reject）
    assert [(c["method"], c["url"]) for c in capture.calls] == [
        ("GET", DIAGNOSE_BASE + "/status/sess_abc"),
        ("POST", DIAGNOSE_BASE + "/remediate/sess_abc"),
        ("POST", DIAGNOSE_BASE + "/approve/sess_abc"),
    ]
    assert capture.calls[1]["json"] == {"option_index": 2}
    assert capture.calls[2]["json"] == {
        "decision": "reject",
        "feedback": "DB 运维确认正常，请复核配置指向",
    }

    items = _decisions(client)
    assert len(items) == 1
    it = items[0]
    assert it["type"] == "diagnose_decision"
    assert it["decision"] == "reject"
    assert it["option_index"] == 2
    assert it["option_title"] == "重启依赖 Db"
    assert it["steps"][0]["target"] == "Deployment payment-db"  # 历史计划的唯一留存处
    assert it["feedback"] == "DB 运维确认正常，请复核配置指向"
    assert it["reanalyze_count"] == 1
    assert it["decided_at"]

    # 快照 = 第一轮的全部信息（根因/总结/方案/分析链路），事后 spike 侧取不到
    snap = it["snapshot"]
    assert snap["status"] == "completed"
    assert snap["conclusion"]["root_cause"] == "NPE on assignee"
    assert snap["conclusion"]["summary"] == "assignee 为空导致 NPE"
    assert snap["tasks"][0]["title"] == "查日志"
    assert snap["tool_calls"][0]["name"] == "git_blame"


def test_reject_without_option_index_omits_the_field(client):
    _seed_log_record(client)
    _bind_session(client)
    capture = CapturingHttp([_status_snap(), _remediate_ok(), _approve_reject_ok()])
    client.app.state.http_client = capture
    assert (
        client.post(
            "/v1/problems/PR-0001/diagnose/decision",
            json={"decision": "reject", "feedback": "再查一遍"},
        ).status_code
        == 200
    )
    assert capture.calls[0]["url"] == DIAGNOSE_BASE + "/status/sess_abc"
    assert capture.calls[1]["json"] == {}  # 缺省 → spike 侧选推荐方案


def test_reject_empty_feedback_400_no_outbound(client):
    _seed_log_record(client)
    _bind_session(client)
    capture = CapturingHttp([_remediate_ok(), _approve_reject_ok()])
    client.app.state.http_client = capture
    resp = client.post(
        "/v1/problems/PR-0001/diagnose/decision",
        json={"decision": "reject", "feedback": "   "},
    )
    assert resp.status_code == 400  # VALIDATION
    assert capture.calls == []  # 入参校验在抓快照之前 → 不为注定 400 的请求出站
    assert _decisions(client) == []


def test_reject_expired_session_409(client):
    _seed_log_record(client)
    _bind_session(client)
    capture = CapturingHttp(
        [_status_snap(), _resp(404, {"detail": "session not found or expired"})]
    )
    client.app.state.http_client = capture
    resp = client.post(
        "/v1/problems/PR-0001/diagnose/decision",
        json={"decision": "reject", "feedback": "再查"},
    )
    assert resp.status_code == 409
    # 抓了 /status，随后 /remediate 404 即停
    assert [c["url"] for c in capture.calls] == [
        DIAGNOSE_BASE + "/status/sess_abc",
        DIAGNOSE_BASE + "/remediate/sess_abc",
    ]
    assert _decisions(client) == []  # 未 append evidence


def test_reject_status_404_still_decides_without_snapshot(client):
    """``/status`` 抓不到（会话已过期）不能阻断决策——降级 snapshot=None。"""
    _seed_log_record(client)
    _bind_session(client)
    capture = CapturingHttp(
        [
            _resp(404, {"detail": "expired"}, method="GET", url=DIAGNOSE_BASE + "/status/sess_abc"),
            _remediate_ok(option_index=1),
            _approve_reject_ok(),
        ]
    )
    client.app.state.http_client = capture
    resp = client.post(
        "/v1/problems/PR-0001/diagnose/decision",
        json={"decision": "reject", "feedback": "再查", "option_index": 1},
    )
    assert resp.status_code == 200
    it = _decisions(client)[0]
    assert it["snapshot"] is None
    assert it["steps"][0]["target"] == "Deployment payment-db"  # 降级路径仍靠 steps


# ======================================================================
# ignore
# ======================================================================
def test_ignore_dismisses_session_and_closes_record(client):
    _seed_log_record(client)
    _bind_session(client)
    capture = CapturingHttp([_status_snap(), _dismiss_ok()])
    client.app.state.http_client = capture
    resp = client.post("/v1/problems/PR-0001/diagnose/decision", json={"decision": "ignore"})
    assert resp.status_code == 200
    assert resp.json()["session_status"] == "dismissed"
    assert resp.json()["record_state"] == "closed"

    assert [(c["method"], c["url"]) for c in capture.calls] == [
        ("GET", DIAGNOSE_BASE + "/status/sess_abc"),
        ("POST", DIAGNOSE_BASE + "/dismiss/sess_abc"),
    ]
    assert capture.calls[1]["json"] == {"reason": "ignored"}

    rec = client.get("/v1/problems/PR-0001").json()
    assert rec["state"] == "closed"
    assert rec["resolve_reason"] == "ignored"
    items = _decisions(client)
    assert len(items) == 1 and items[0]["decision"] == "ignore"
    assert items[0]["snapshot"]["conclusion"]["root_cause"] == "NPE on assignee"  # 三态都存快照


def test_ignore_tolerates_expired_session_and_still_closes(client):
    """会话过期（spike 404）不能阻断关单——不制造 404 死胡同；快照同步降级为 None。"""
    _seed_log_record(client)
    _bind_session(client)
    capture = CapturingHttp(_resp(404, {"detail": "session not found or expired"}))
    client.app.state.http_client = capture
    resp = client.post("/v1/problems/PR-0001/diagnose/decision", json={"decision": "ignore"})
    assert resp.status_code == 200
    assert resp.json()["session_status"] == "expired"
    assert resp.json()["record_state"] == "closed"
    assert client.get("/v1/problems/PR-0001").json()["state"] == "closed"
    it = _decisions(client)[0]
    assert it["session_status"] == "expired"
    assert it["snapshot"] is None  # /status 与 /dismiss 同走 404 → 降级


# ======================================================================
# false_positive
# ======================================================================
def test_false_positive_dismisses_records_fpr_and_resolves(client):
    _seed_log_record(client)
    _bind_session(client)
    client.app.state.http_client = CapturingHttp([_status_snap(), _dismiss_ok(reason="false_positive")])
    resp = client.post(
        "/v1/problems/PR-0001/diagnose/decision", json={"decision": "false_positive"}
    )
    assert resp.status_code == 200
    assert resp.json()["record_state"] == "resolved"

    rec = client.get("/v1/problems/PR-0001").json()
    assert rec["state"] == "resolved"
    assert rec["resolve_reason"] == "false_positive"

    # FPR 回写（参照 tests/test_fpr_writeback.py）
    fpr = asyncio.run(client.app.state.storage.dynamic_config.load_fpr("default"))
    assert fpr[rec["group_key"]]["total"] == 1
    assert fpr[rec["group_key"]]["fpr"] == 1.0
    assert FALSE_POSITIVE_RATE.labels(rec["service"])._value.get() == 1.0

    items = _decisions(client)
    assert len(items) == 1 and items[0]["decision"] == "false_positive"


# ======================================================================
# 守卫：未绑定 / 已终态 / 未知记录
# ======================================================================
def test_decision_without_binding_409_no_outbound(client):
    _seed_log_record(client)
    capture = CapturingHttp(_ok_resp())
    client.app.state.http_client = capture
    resp = client.post("/v1/problems/PR-0001/diagnose/decision", json={"decision": "ignore"})
    assert resp.status_code == 409
    assert capture.calls == []


def test_decision_on_terminal_record_409_no_outbound(client):
    _seed_log_record(client)
    _bind_session(client)
    client.post("/v1/problems/PR-0001/resolve")

    capture = CapturingHttp(_resp(200, {"status": "dismissed"}))
    client.app.state.http_client = capture
    resp = client.post("/v1/problems/PR-0001/diagnose/decision", json={"decision": "ignore"})
    assert resp.status_code == 409
    assert capture.calls == []


def test_decision_unknown_record_404(client):
    assert (
        client.post("/v1/problems/PR-9999/diagnose/decision", json={"decision": "ignore"}).status_code
        == 404
    )


# ======================================================================
# 历史读取
# ======================================================================
def test_decisions_empty_when_none(client):
    _seed_log_record(client)
    _bind_session(client)
    assert _decisions(client) == []


def test_decisions_returns_in_write_order(client):
    _seed_log_record(client)
    _bind_session(client)
    client.app.state.http_client = CapturingHttp([_status_snap(), _dismiss_ok()])
    client.post("/v1/problems/PR-0001/diagnose/decision", json={"decision": "ignore"})
    # 同一条记录再记一次误报（状态已 closed → 被 409 拦；直接验证读取端按序返回即可）
    items = _decisions(client)
    assert [it["decision"] for it in items] == ["ignore"]

    # 另一条记录：多次决策按写入顺序返回
    _seed_log_record(client, record_id="PR-0002", service="svc-b")
    _bind_session(client, record_id="PR-0002", session_id="sess_b")
    client.app.state.http_client = CapturingHttp(
        [_status_snap(session_id="sess_b"), _remediate_ok(option_index=1), _approve_reject_ok(count=1)]
    )
    client.post(
        "/v1/problems/PR-0002/diagnose/decision",
        json={"decision": "reject", "feedback": "先查配置", "option_index": 1},
    )
    client.app.state.http_client = CapturingHttp(
        [_status_snap(session_id="sess_b"), _dismiss_ok(session_id="sess_b")]
    )
    client.post("/v1/problems/PR-0002/diagnose/decision", json={"decision": "ignore"})
    assert [it["decision"] for it in _decisions(client, "PR-0002")] == ["reject", "ignore"]


def test_decisions_unknown_record_404(client):
    assert client.get("/v1/problems/PR-9999/diagnose/decisions").status_code == 404


# ======================================================================
# 列表剥离：快照只在详情与 /diagnose/decisions，列表不带（5s 轮询背不动）
# ======================================================================
def _evidence_of(rec, etype="diagnose_decision"):
    return [e for e in rec.get("evidence") or [] if e.get("type") == etype]


def test_list_strips_snapshot_but_detail_keeps_it(client):
    _seed_log_record(client)
    _bind_session(client)
    client.app.state.http_client = CapturingHttp([_status_snap(), _dismiss_ok()])
    assert (
        client.post("/v1/problems/PR-0001/diagnose/decision", json={"decision": "ignore"}).status_code
        == 200
    )

    listed = client.get("/v1/problems").json()["items"][0]
    entry = _evidence_of(listed)[0]
    assert entry["type"] == "diagnose_decision"
    assert entry["decision"] == "ignore"
    assert "snapshot" not in entry  # 列表剥离

    detail = client.get("/v1/problems/PR-0001").json()
    assert _evidence_of(detail)[0]["snapshot"]["conclusion"]["root_cause"] == "NPE on assignee"

    # 只读历史端点也返回全量
    assert _decisions(client)[0]["snapshot"]["status"] == "completed"


def test_list_without_decisions_unchanged(client):
    _seed_log_record(client)
    listed = client.get("/v1/problems").json()["items"][0]
    assert _evidence_of(listed) == []  # 没有决策条目 → 原样返回


def test_strip_decision_snapshots_does_not_mutate_input():
    from aiops_apm.router.problems import _strip_decision_snapshots

    rec = {
        "record_id": "PR-0001",
        "evidence": [
            {"type": "diagnose_session", "session_id": "s"},
            {"type": "diagnose_decision", "decision": "ignore", "snapshot": {"a": 1}},
        ],
    }
    out = _strip_decision_snapshots(rec)
    assert "snapshot" not in out["evidence"][1]
    assert rec["evidence"][1]["snapshot"] == {"a": 1}  # 源记录不动
    assert out["evidence"][0] == {"type": "diagnose_session", "session_id": "s"}


# ======================================================================
# 体积护栏（纯函数）
# ======================================================================
def test_bounded_snapshot_truncates_supporting_text():
    from aiops_apm.router.problems import _bounded_snapshot

    long_text = "x" * 5000
    snap = {"conclusion": {"evidence": [{"source": "a.log", "supporting_text": long_text}]}}
    out = _bounded_snapshot(snap)
    assert len(out["conclusion"]["evidence"][0]["supporting_text"]) == 4000


def test_bounded_snapshot_drops_tool_calls_when_oversized():
    from aiops_apm.router.problems import _bounded_snapshot

    snap = {
        "conclusion": {"root_cause": "boom"},
        "tasks": [{"id": "t1"}],
        "tool_calls": [{"name": "n", "result": "y" * (200 * 1024)}],
    }
    out = _bounded_snapshot(snap)
    assert "tool_calls" not in out
    assert out["conclusion"]["root_cause"] == "boom"  # 结论/任务保留
    assert out["tasks"] == [{"id": "t1"}]
