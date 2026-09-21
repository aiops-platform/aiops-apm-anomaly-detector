"""工单回传：``POST /v1/problems/ticket-status``。

链路是 本仓 escalate 派单 → agentflow 跑修复工作流 → MCP 工具
``returnApmTicketStatus`` POST 回来。**这是本仓唯一由外部系统（而非人）驱动的
工单写面**，所以三条性质必须钉住：

1. **按工单号反查**（回传方手里没有 ``record_id``）；
2. **幂等**（agentflow 换轮次 / resume 会重放同一次回传）；
3. **人的裁定优先**（一次迟到的回传不能覆盖人刚做的判断）。
"""

import asyncio
from datetime import datetime, timezone

from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.record import Correlation, ProblemRecord, Verification

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)

TICKET = "INC-20260826-0007"
#: agentflow 内部 ticket id —— 与工单号是两个不同的东西，反查时也认（见 find_by_ticket）
AGENTFLOW_TID = "t_9f3c11"


async def _seed(store, *, record_id="PR-0001", tenant="default", ticket=TICKET, escalated=True):
    anomaly = MetricAnomaly(
        service="order-service", metric="cpu_usage", value=0.95, method="static_threshold",
        severity="high", detected_at=TS,
    )
    rec = ProblemRecord(
        record_id=record_id,
        # ⚠️ 必须写在**模型**上：`write_or_append` 落库走的是 `record.model_dump()`，
        # 传参 tenant_id 只用于去重比对，不写进行里。只传参的话记录会落在
        # 模型的默认租户下，而 `get(tenant, …)` 按行里的 tenant_id 过滤 → 查不到。
        tenant_id=tenant,
        domain="application",
        service="order-service",
        severity="high",
        detected_at=TS,
        symptom={"summary": "order-service cpu_usage 0.95"},
        metric_anomalies=[anomaly],
        log_anomalies=[],
        correlation=Correlation(related=False, reason="metric_only"),
        verification=Verification(passed=True, persistence_ok=True, final_severity="high"),
    )
    await store.records.write_or_append(tenant, rec)
    if escalated:
        # 走真实路径写入：escalate 那一步记的 evidence + mark_escalated 写的 reason
        await store.records.append_evidence(
            tenant, record_id,
            {"type": "diagnose_decision", "decision": "escalate",
             "ticket_id": AGENTFLOW_TID, "ticket_number": ticket},
        )
        await store.records.mark_escalated(tenant, record_id, reason=f"escalated:{ticket}")


def _post(client, body, tenant=None):
    headers = {"X-Tenant-Id": tenant} if tenant else {}
    return client.post("/v1/problems/ticket-status", json=body, headers=headers)


def _rec(client, record_id="PR-0001", tenant="default"):
    return asyncio.run(client.app.state.storage.records.get(tenant, record_id))


def _evidence_types(rec):
    return [e.get("type") for e in (rec.get("evidence") or []) if isinstance(e, dict)]


# ── 反查 + 状态映射 ──────────────────────────────────────────────────
def test_resolved_closes_the_escalated_problem(client):
    """`resolved` ⇒ 问题单从 escalated 落到 resolved，reason 里带工单号。"""
    asyncio.run(_seed(client.app.state.storage))

    resp = _post(client, {"ticket_id": TICKET, "status": "resolved",
                          "description": "根因：数据盘写满，已提交修复"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["record_id"] == "PR-0001"
    assert body["state"] == "resolved" and body["state_changed"] is True

    rec = _rec(client)
    assert rec["state"] == "resolved"
    assert rec["resolve_reason"] == f"agentflow:resolved:{TICKET}"


def test_failed_keeps_escalated(client):
    """`failed` ⇒ **只追加证据、不动状态**——单子还在修，不该被编造出一个结论。"""
    asyncio.run(_seed(client.app.state.storage))

    resp = _post(client, {"ticket_id": TICKET, "status": "failed", "description": "测试没过"})
    assert resp.status_code == 200
    assert resp.json()["state_changed"] is False

    rec = _rec(client)
    assert rec["state"] == "escalated"
    assert "ticket_status" in _evidence_types(rec)


def test_insufficient_keeps_escalated(client):
    asyncio.run(_seed(client.app.state.storage))
    resp = _post(client, {"ticket_id": TICKET, "status": "insufficient",
                          "description": "证据不足，没定位到可改的仓库"})
    assert resp.status_code == 200
    assert _rec(client)["state"] == "escalated"


def test_lookup_also_accepts_agentflow_internal_id(client):
    """回传方拿 agentflow 内部 id 也认——它确实是"这张工单"，只是另一个编号体系。"""
    asyncio.run(_seed(client.app.state.storage))
    resp = _post(client, {"ticket_id": AGENTFLOW_TID, "status": "failed", "description": "x"})
    assert resp.status_code == 200
    assert resp.json()["record_id"] == "PR-0001"


def test_unknown_ticket_is_404(client):
    """本租户没派出去过的号 ⇒ 404。**不建单、不猜**。"""
    asyncio.run(_seed(client.app.state.storage))
    resp = _post(client, {"ticket_id": "INC-20260101-9999", "status": "resolved",
                          "description": "x"})
    assert resp.status_code == 404
    assert _rec(client)["state"] == "escalated"  # 原单未被误动


def test_not_escalated_record_is_not_found(client):
    """没派过单的问题单（没有工单）不该被回传命中——否则任意单号都能改别人的状态。"""
    asyncio.run(_seed(client.app.state.storage, escalated=False))
    resp = _post(client, {"ticket_id": TICKET, "status": "resolved", "description": "x"})
    assert resp.status_code == 404


# ── 幂等 ────────────────────────────────────────────────────────────
def test_replay_appends_evidence_once(client):
    """同一次回传重放 ⇒ 不重复追加证据（agentflow 换轮次 / resume 会重放）。

    ⚠️ 判据里**排除时间戳**：带上 ``reported_at`` 的话这条永远为假，幂等形同虚设，
    而症状恰好是"看不出问题"——重放时多一条证据，界面照常渲染。
    """
    asyncio.run(_seed(client.app.state.storage))
    body = {"ticket_id": TICKET, "status": "failed", "description": "测试没过"}

    first = _post(client, body)
    second = _post(client, body)
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True

    assert _evidence_types(_rec(client)).count("ticket_status") == 1


def test_different_status_is_not_a_duplicate(client):
    """换了结论的重报不是重放，必须记下来（第一次 failed、修好后 resolved 是正常路径）。"""
    asyncio.run(_seed(client.app.state.storage))
    _post(client, {"ticket_id": TICKET, "status": "failed", "description": "测试没过"})
    _post(client, {"ticket_id": TICKET, "status": "resolved", "description": "测试没过"})

    rec = _rec(client)
    assert _evidence_types(rec).count("ticket_status") == 2
    assert rec["state"] == "resolved"


def test_replay_after_resolve_does_not_rewrite(client):
    """已 resolved 之后重放同一条 ⇒ duplicate，且状态不再被写一遍。"""
    asyncio.run(_seed(client.app.state.storage))
    body = {"ticket_id": TICKET, "status": "resolved", "description": "根因：数据盘写满"}
    _post(client, body)
    resp = _post(client, body)
    assert resp.json() == {
        "ok": True, "duplicate": True, "record_id": "PR-0001",
        "state": "resolved", "state_changed": False,
    }


# ── 人的裁定优先 ────────────────────────────────────────────────────
def test_human_close_wins_over_late_callback(client):
    """人已经把单子「忽略」关掉了，一次迟到的 resolved 回传**不能**把它翻回 resolved。

    反过来的话，回传会覆盖人刚做的判断——而人看到的是"我明明点了忽略，它怎么自己
    变成已修复了"。
    """
    asyncio.run(_seed(client.app.state.storage))
    asyncio.run(client.app.state.storage.records.close("default", "PR-0001", reason="ignored"))

    resp = _post(client, {"ticket_id": TICKET, "status": "resolved", "description": "修好了"})
    assert resp.status_code == 200
    assert resp.json()["state_changed"] is False

    rec = _rec(client)
    assert rec["state"] == "closed", "人的裁定不能被回传改写"
    assert rec["resolve_reason"] == "ignored"
    assert "ticket_status" in _evidence_types(rec), "但证据要留下"


# ── 租户隔离 ────────────────────────────────────────────────────────
def test_callback_is_tenant_scoped(client):
    """别的租户的工单号在本租户查不到——多租户隔离由构造保证。"""
    asyncio.run(_seed(client.app.state.storage, tenant="team-a"))
    resp = _post(client, {"ticket_id": TICKET, "status": "resolved", "description": "x"},
                 tenant="team-b")
    assert resp.status_code == 404
    assert _rec(client, tenant="team-a")["state"] == "escalated"
