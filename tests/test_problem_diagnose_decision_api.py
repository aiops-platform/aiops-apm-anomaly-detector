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


# ======================================================================
# agentflow 路径：升级（放行门 → 建工单 → 绑号 → 终态）
# ======================================================================
AGENTFLOW_BASE = "http://localhost:8000"


def _bind_agent_run(client, *, record_id="PR-0001", run_id="run_abc", workflow_id="wf-1"):
    """把记录推到 ``in_progress`` 并绑上 ``agent_run`` evidence——决策端点据此分派 agentflow 路径。"""
    client.app.state.http_client = CapturingHttp(
        _resp(200, {"run_id": run_id, "status": "started"}, url=AGENTFLOW_BASE + "/run")
    )
    assert (
        client.post(
            f"/v1/problems/{record_id}/analyze", json={"workflow_id": workflow_id}
        ).status_code
        == 200
    )


def _run_detail(*, run_id="run_abc", gate="diagnose-output"):
    """``GET /runs/{id}``：门的动态发现来源，同时供工单里的诊断摘要。

    ``gate=None`` → ``pending_approvals`` 为空（门早被答复 / run 的状态列陈旧）。
    """
    return _resp(
        200,
        {
            "run_id": run_id,
            "status": "waiting_approval",
            "pending_approvals": [] if gate is None else [{"node_id": gate, "trigger": None, "upstream": {"rca": {}}}],
            "nodes": {
                "rca": {
                    "status": "done",
                    "output": {
                        "hypotheses": ["template 为 null 时缺少空值校验"],
                        "confidence": 0.9,
                    },
                },
                "plan": {
                    "status": "done",
                    "output": {
                        "summary": "补空值校验",
                        "steps": [{"type": "code_fix", "target": "QuotationService.java"}],
                    },
                },
            },
        },
        method="GET",
        url=AGENTFLOW_BASE + f"/runs/{run_id}",
    )


def _approve_ok(*, run_id="run_abc"):
    return _resp(
        200,
        {"approval": {"status": "APPROVED", "approved": True}, "run_status": "done"},
        url=AGENTFLOW_BASE + f"/runs/{run_id}/approve",
    )


def _ticket_ok(ticket_id="tkt123456789", number="INC-20260921-0001"):
    return _resp(
        201,
        {"id": ticket_id, "number": number, "title": "x", "status": "new", "run_ids": []},
        url=AGENTFLOW_BASE + "/tickets",
    )


def test_escalate_approves_gate_creates_ticket_and_marks_escalated(client):
    """升级：先放行门、再建工单、最后置终态——三件事都要发生，且顺序固定。"""
    _seed_log_record(client)
    _bind_agent_run(client)
    capture = CapturingHttp([_run_detail(), _approve_ok(), _ticket_ok()])
    client.app.state.http_client = capture

    resp = client.post("/v1/problems/PR-0001/diagnose/decision", json={"decision": "escalate"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "escalate"
    assert body["record_state"] == "escalated"
    assert body["ticket_id"] == "tkt123456789"
    assert body["ticket_number"].startswith("INC-")  # 号由本仓生成，用现成的 SequenceStore
    assert body["session_status"] == "escalated"

    # 出站顺序：读 run（发现门 + 取摘要）→ 放行门 → 建单。
    # **先放行后建单**：反过来的话，"建单成功而 approve 失败"会留下一张已派单、run 还挂在
    # 门上的记录——而挂在门上的 run 一直占着租户并发额度。
    assert [(c["method"], c["url"]) for c in capture.calls] == [
        ("GET", AGENTFLOW_BASE + "/runs/run_abc"),
        ("POST", AGENTFLOW_BASE + "/runs/run_abc/approve"),
        ("POST", AGENTFLOW_BASE + "/tickets"),
    ]
    # 门节点从 pending_approvals 现取，不是写死的常量（改名后写死的那份会静默打偏）
    assert capture.calls[1]["json"]["node_id"] == "diagnose-output"

    payload = capture.calls[2]["json"]
    assert payload["number"].startswith("INC-")
    assert payload["service"] == "sip-aiops-management"
    assert payload["severity"] == "high"
    assert payload["window_start"] and payload["window_end"]
    # 诊断摘要随工单走：拿到工单的人不必回平台翻诊断。与页面同源（复用 _build_conclusion）
    diag = payload["bug_report"]["diagnosis"]
    assert diag["root_cause"] == "template 为 null 时缺少空值校验"
    assert diag["summary"] == "补空值校验"
    assert diag["recommended_fix"][0]["steps"][0]["target"] == "QuotationService.java"

    # state 落库 + evidence 绑号（工单号是"不查 evidence 就能看到"的那一份，写在 resolve_reason）
    detail = client.get("/v1/problems/PR-0001").json()
    assert detail["state"] == "escalated"
    assert detail["resolve_reason"].startswith("escalated:INC-")
    # 「升级」与「它建出的工单」是**同一件事**，落**一条** evidence（不是两条）
    created = [e for e in detail["evidence"] if e.get("decision") == "escalate"]
    assert len(created) == 1
    assert created[0]["type"] == "diagnose_decision"
    assert created[0]["ticket_id"] == "tkt123456789"
    assert created[0]["ticket_number"] == payload["number"]


def test_escalate_discovers_gate_node_from_pending_approvals(client):
    """门节点 id **以 run 的 pending_approvals 为准**——这是节点改名的唯一保险。

    写死的那份（原 ``approve-plan``，现 ``diagnose-output``）只在发现失败时兜底：
    打到一个不存在的节点时 agentflow 报 400、run 永远停在 ``waiting_approval``，
    而调用方看到的只是"这次操作失败了"，看不出是节点名对不上。
    """
    _seed_log_record(client)
    _bind_agent_run(client)
    capture = CapturingHttp([_run_detail(gate="some-renamed-gate"), _approve_ok(), _ticket_ok()])
    client.app.state.http_client = capture

    assert (
        client.post("/v1/problems/PR-0001/diagnose/decision", json={"decision": "escalate"}).status_code
        == 200
    )
    assert capture.calls[1]["json"]["node_id"] == "some-renamed-gate"


def test_escalate_is_idempotent_when_ticket_already_recorded(client):
    """已有升级裁定 → **不再建单、也不再放行门**，直接回放。

    重试路径真实存在：工单建出来但 ``append_evidence`` 失败时，人看到的是报错，
    而单已经在 agentflow 里了。没有这道前置就会建出第二张。
    """
    _seed_log_record(client)
    _bind_agent_run(client)
    store = client.app.state.storage.records
    asyncio.run(
        store.append_evidence(
            "default",
            "PR-0001",
            {
                "type": "diagnose_decision",
                "decision": "escalate",
                "ticket_id": "tkt_old",
                "ticket_number": "INC-20260920-0007",
            },
        )
    )
    capture = CapturingHttp([_run_detail(), _approve_ok(), _ticket_ok()])
    client.app.state.http_client = capture

    body = client.post(
        "/v1/problems/PR-0001/diagnose/decision", json={"decision": "escalate"}
    ).json()
    assert body["ticket_id"] == "tkt_old"
    assert body["ticket_number"] == "INC-20260920-0007"
    assert body["remediation_status"] == "already_escalated"
    assert body["record_state"] == "escalated"
    # 只读了 run，**没有** approve、**没有** 建单
    assert [c["url"] for c in capture.calls] == [AGENTFLOW_BASE + "/runs/run_abc"]


def test_escalate_ticket_failure_leaves_record_in_progress(client):
    """建单失败 → 502 且**不置终态**：没派出单就不该对外说"已升级"，人可以重试。"""
    _seed_log_record(client)
    _bind_agent_run(client)
    client.app.state.http_client = CapturingHttp(
        [_run_detail(), _approve_ok(), _resp(500, {"detail": "boom"}, url=AGENTFLOW_BASE + "/tickets")]
    )

    resp = client.post("/v1/problems/PR-0001/diagnose/decision", json={"decision": "escalate"})
    assert resp.status_code == 502
    detail = client.get("/v1/problems/PR-0001").json()
    assert detail["state"] == "in_progress"  # 没落终态
    assert not [e for e in detail["evidence"] if e.get("decision") == "escalate"]


def test_escalate_on_already_escalated_record_is_conflict(client):
    """终态守卫：已升级的单不能再裁定（否则会重复派单）。"""
    _seed_log_record(client)
    _bind_agent_run(client)
    client.app.state.http_client = CapturingHttp([_run_detail(), _approve_ok(), _ticket_ok()])
    assert (
        client.post("/v1/problems/PR-0001/diagnose/decision", json={"decision": "escalate"}).status_code
        == 200
    )
    resp = client.post("/v1/problems/PR-0001/diagnose/decision", json={"decision": "escalate"})
    assert resp.status_code == 409


def test_escalate_records_decision_in_history(client):
    """历史（View Diagnosis 的「历史计划」区）要能看到这一次升级与工单号。"""
    _seed_log_record(client)
    _bind_agent_run(client)
    client.app.state.http_client = CapturingHttp([_run_detail(), _approve_ok(), _ticket_ok()])
    client.post("/v1/problems/PR-0001/diagnose/decision", json={"decision": "escalate"})

    items = _decisions(client)
    assert len(items) == 1
    assert items[0]["decision"] == "escalate"
    assert items[0]["engine"] == "agentflow"
    assert items[0]["ticket_number"].startswith("INC-")


def test_spike_escalate_dismisses_session_creates_ticket_and_marks_escalated(client):
    """老路径（spike 会话）同样支持升级：没有 agentflow 门可放行，其余同构。

    dismiss 在这里是 **best-effort**：``reason`` 的枚举属于 spike 服务（另一个仓），
    ``escalated`` 是猜的——猜错不该让"工单已经建出来"回滚，失败只记进 ``session_status``。
    """
    _seed_log_record(client)
    _bind_session(client)
    client.app.state.http_client = CapturingHttp(
        [
            _status_snap(),
            _dismiss_ok(reason="escalated"),
            _ticket_ok(ticket_id="tkt_spike", number="INC-20260921-0002"),
        ]
    )

    body = client.post(
        "/v1/problems/PR-0001/diagnose/decision", json={"decision": "escalate"}
    ).json()
    assert body["ticket_id"] == "tkt_spike"
    assert body["session_status"] == "dismissed"
    assert body["record_state"] == "escalated"
    assert client.get("/v1/problems/PR-0001").json()["state"] == "escalated"


def test_spike_escalate_tolerates_dismiss_rejection(client):
    """spike 不认 escalated 这个 reason（4xx）时**仍要完成升级**，并把失败如实记进历史。"""
    _seed_log_record(client)
    _bind_session(client)
    client.app.state.http_client = CapturingHttp(
        [
            _status_snap(),
            _resp(400, {"detail": "unknown reason"}, url=DIAGNOSE_BASE + "/dismiss/sess_abc"),
            _ticket_ok(ticket_id="tkt_spike2", number="INC-20260921-0003"),
        ]
    )

    body = client.post(
        "/v1/problems/PR-0001/diagnose/decision", json={"decision": "escalate"}
    ).json()
    assert body["ticket_id"] == "tkt_spike2"
    assert body["record_state"] == "escalated"
    # 失败**不静默**：写在 session_status 里，历史可见
    assert body["session_status"].startswith("dismiss_failed")


def test_escalate_without_pending_gate_skips_approve(client):
    """run 上没有在等的门 → **不发那次 approve**，直接建单、置终态。

    必须跳过而不是"猜一个节点 id 打过去"：run 的**状态列可能是陈旧的 `waiting_approval`**
    （checkpoint 才是"门开着没有"的真源，本仓库里实测就有一条这样的记录），而猜的 id 打到
    agentflow 时，`DAGExecutor.approve` 第一行 `self.dag.nodes[nid]` 对不存在的节点抛
    `KeyError` —— 没进异常映射 → **HTTP 500**（"节点存在但不在等待"才是干净的 400，
    那个 KeyError 是 agentflow 侧的既有缺陷）。一次注定的失败出站，还只会在 evidence 里
    留一句读不懂的 "HTTP 500"。

    （这条是**实跑真环境时发现的**：升级一条门早已被答复、但状态列仍写着 waiting_approval
    的记录，evidence 里落的就是 `failed: HTTP 500 Internal Server Error`。）
    """
    _seed_log_record(client)
    _bind_agent_run(client)
    capture = CapturingHttp([_run_detail(gate=None), _ticket_ok(ticket_id="tkt_nogate")])
    client.app.state.http_client = capture

    body = client.post(
        "/v1/problems/PR-0001/diagnose/decision", json={"decision": "escalate"}
    ).json()
    assert body["ticket_id"] == "tkt_nogate"
    assert body["remediation_status"] == "no_pending_gate"
    assert body["record_state"] == "escalated"
    # **只有两次出站**：读 run + 建单。approve 那次被跳过了
    assert [c["url"] for c in capture.calls] == [
        AGENTFLOW_BASE + "/runs/run_abc",
        AGENTFLOW_BASE + "/tickets",
    ]
