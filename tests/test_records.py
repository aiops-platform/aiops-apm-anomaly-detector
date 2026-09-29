"""UC-2.2/2.3/2.4 problem_record 落库：新开 / 追加去重 / 已解决复发开新单。

以 InMemoryRecordStore 为单测真源（PG 实现语义一致，生产用原子去重）。
"""

from datetime import datetime, timezone

import pytest

from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.record import Correlation, ProblemRecord, Verification
from aiops_apm.storage.records import InMemoryRecordStore

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
NOW2 = datetime(2026, 8, 26, 12, 5, 0, tzinfo=timezone.utc)


def _anomaly(
    service: str = "order-management",
    severity: str = "high",
    tenant_id: str = "default",
    metric: str = "cpu_usage",
) -> MetricAnomaly:
    return MetricAnomaly(
        kind="metric",
        tenant_id=tenant_id,
        service=service,
        metric=metric,
        value=0.95,
        baseline=0.5,
        method="static_threshold",
        severity=severity,
        detected_at=NOW,
        labels={},
    )


def _record(
    record_id: str,
    *,
    tenant_id: str = "default",
    service: str = "order-management",
    severity: str = "high",
    evidence: list[dict] | None = None,
    last_seen_at: datetime | None = None,
    metrics: list[str] | None = None,
) -> ProblemRecord:
    anoms = [
        _anomaly(service=service, severity=severity, tenant_id=tenant_id, metric=m)
        for m in (metrics or ["cpu_usage"])
    ]
    return ProblemRecord(
        record_id=record_id,
        tenant_id=tenant_id,
        domain="application",
        state="pending",
        service=service,
        severity=severity,
        detected_at=NOW,
        first_seen_at=NOW,
        last_seen_at=last_seen_at or NOW,
        occurrence_count=1,
        symptom={"summary": "cpu spike", "severity": severity},
        metric_anomalies=anoms,
        log_anomalies=[],
        correlation=Correlation(related=False, reason=""),
        verification=Verification(passed=True, persistence_ok=True, final_severity=severity),
        evidence=evidence if evidence is not None else [],
    )


async def test_uc22_write_new_record() -> None:
    store = InMemoryRecordStore()
    r = _record(record_id="PR-0001")
    await store.write_or_append("default", r)

    rows = await store.list("default")
    assert len(rows) == 1
    assert rows[0]["record_id"] == "PR-0001"
    assert rows[0]["state"] == "pending"

    opened = await store.find_open("default", r.group_key)
    assert opened is not None
    assert opened["record_id"] == "PR-0001"


async def test_uc23_append_evidence_on_dup() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001"))
    await store.write_or_append("default", _record("PR-0002", evidence=[{"k": "extra"}], last_seen_at=NOW2))

    rows = await store.list("default")
    assert len(rows) == 1  # 同 group_key 不重复开单
    row = rows[0]
    assert row["record_id"] == "PR-0001"  # 保留原单
    assert row["occurrence_count"] == 2
    assert row["evidence"] == [{"k": "extra"}]
    assert row["last_seen_at"] == NOW2


async def test_uc23_severity_only_upgrades() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001", severity="warning"))
    await store.write_or_append("default", _record("PR-0002", severity="critical"))
    assert (await store.list("default"))[0]["severity"] == "critical"

    # 降级不改写已存在的更高严重度
    await store.write_or_append("default", _record("PR-0003", severity="high"))
    assert (await store.list("default"))[0]["severity"] == "critical"


async def test_uc24_resolved_record_reopens_new() -> None:
    store = InMemoryRecordStore()
    r1 = _record("PR-0001")
    await store.write_or_append("default", r1)
    await store.resolve("default", "PR-0001", reason="auto-recovered")
    assert await store.find_open("default", r1.group_key) is None

    r2 = _record("PR-0002")
    await store.write_or_append("default", r2)
    rows = await store.list("default")
    assert len(rows) == 2
    opened = await store.find_open("default", r2.group_key)
    assert opened is not None
    assert opened["record_id"] == "PR-0002"


async def test_tenant_isolation() -> None:
    store = InMemoryRecordStore()
    r = _record("PR-0001", tenant_id="t1")
    await store.write_or_append("t1", r)
    assert await store.find_open("t2", r.group_key) is None
    assert await store.list("t2") == []


async def test_methods_require_tenant_id() -> None:
    store = InMemoryRecordStore()
    r = _record("PR-0001")
    with pytest.raises(ValueError):
        await store.write_or_append("", r)
    with pytest.raises(ValueError):
        await store.find_open("", r.group_key)
    with pytest.raises(ValueError):
        await store.list("")
    with pytest.raises(ValueError):
        await store.resolve("", "PR-0001")


async def test_list_filters_by_state_and_service() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001", service="order-management"))
    await store.write_or_append("default", _record("PR-0002", service="payment-service"))
    await store.resolve("default", "PR-0002", reason="auto")

    pending = await store.list("default", state="pending")
    assert [r["record_id"] for r in pending] == ["PR-0001"]
    by_service = await store.list("default", service="payment-service")
    assert [r["record_id"] for r in by_service] == ["PR-0002"]
    resolved = await store.list("default", state="resolved")
    assert [r["record_id"] for r in resolved] == ["PR-0002"]


async def test_list_service_filter_matches_member_of_cross_service_record() -> None:
    """M9：跨服务记录的 service 是逗号拼接串，按**其中任一**服务都要能查到。

    精确相等（原先的 ``r["service"] == service``）会漏掉这类记录——用户按
    ``?service=order-service`` 查不到那条同时涉及 order 与 gateway 的事故。
    """
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001", service="gateway-service,order-service"))
    await store.write_or_append("default", _record("PR-0002", service="payment-service"))

    assert [r["record_id"] for r in await store.list("default", service="order-service")] == ["PR-0001"]
    assert [r["record_id"] for r in await store.list("default", service="gateway-service")] == ["PR-0001"]
    assert [r["record_id"] for r in await store.list("default", service="payment-service")] == ["PR-0002"]
    # 子串不算命中：'order' 不该匹到 'order-service'
    assert await store.list("default", service="order") == []


# ── mark_in_progress：pending → in_progress（agent 分析发起）──────────────────

async def test_mark_in_progress_flips_and_appends_evidence() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001"))
    ok = await store.mark_in_progress("default", "PR-0001", run_id="run_abc", workflow_id="wf-1")
    assert ok is True
    row = await store.get("default", "PR-0001")
    assert row is not None
    assert row["state"] == "in_progress"
    assert row["evidence"][-1]["type"] == "agent_run"
    assert row["evidence"][-1]["run_id"] == "run_abc"
    assert row["evidence"][-1]["workflow_id"] == "wf-1"
    assert "started_at" in row["evidence"][-1]


async def test_mark_in_progress_only_once() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001"))
    assert await store.mark_in_progress("default", "PR-0001", run_id="run_1", workflow_id="wf-1") is True
    # 已在 in_progress：不再翻转、不再追加 evidence
    assert await store.mark_in_progress("default", "PR-0001", run_id="run_2", workflow_id="wf-2") is False
    row = await store.get("default", "PR-0001")
    assert row is not None
    agent_runs = [e for e in row["evidence"] if e.get("type") == "agent_run"]
    assert len(agent_runs) == 1


async def test_mark_in_progress_resolved_record_is_false() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001"))
    await store.resolve("default", "PR-0001", reason="manual")
    assert await store.mark_in_progress("default", "PR-0001", run_id="run_x", workflow_id="wf-1") is False


async def test_mark_in_progress_missing_or_wrong_tenant_is_false() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001", tenant_id="t1"))
    assert await store.mark_in_progress("t2", "PR-0001", run_id="run_x", workflow_id="wf-1") is False
    assert await store.mark_in_progress("t1", "PR-9999", run_id="run_x", workflow_id="wf-1") is False
    with pytest.raises(ValueError):
        await store.mark_in_progress("", "PR-0001", run_id="run_x", workflow_id="wf-1")


# ── close：state=closed（与 resolved 并列的终态，忽略）────────────────────────

async def test_close_sets_closed_and_audit_columns() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001"))
    await store.close("default", "PR-0001", reason="ignored")
    row = await store.get("default", "PR-0001")
    assert row is not None
    assert row["state"] == "closed"
    assert row["resolve_reason"] == "ignored"
    assert isinstance(row["resolved_at"], datetime)


async def test_closed_record_leaves_open_and_reopens_new() -> None:
    store = InMemoryRecordStore()
    r1 = _record("PR-0001")
    await store.write_or_append("default", r1)
    await store.close("default", "PR-0001", reason="ignored")
    assert await store.find_open("default", r1.group_key) is None

    # 复发 → 新开一单，而不是追加进已 closed 的单
    await store.write_or_append("default", _record("PR-0002"))
    rows = await store.list("default")
    assert len(rows) == 2
    opened = await store.find_open("default", r1.group_key)
    assert opened is not None
    assert opened["record_id"] == "PR-0002"


async def test_close_missing_or_wrong_tenant_is_noop() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001", tenant_id="t1"))
    await store.close("t2", "PR-0001", reason="ignored")
    assert (await store.get("t1", "PR-0001"))["state"] == "pending"
    with pytest.raises(ValueError):
        await store.close("", "PR-0001")



# ── mark_escalated：state=escalated（第三个终态，升级派单）────────────────────

async def test_mark_escalated_sets_state_and_audit_columns() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001"))
    await store.mark_escalated("default", "PR-0001", reason="escalated:INC-20260921-0001")
    row = await store.get("default", "PR-0001")
    assert row is not None
    assert row["state"] == "escalated"
    # 与 resolved/closed 复用同一组审计列；工单号带在 reason 里（前端 Detail 的 Resolve Reason 行）
    assert row["resolve_reason"] == "escalated:INC-20260921-0001"
    assert isinstance(row["resolved_at"], datetime)


async def test_escalated_record_leaves_open_and_reopens_new() -> None:
    """升级是终态 ⇒ 复发开新单（与 resolved/closed 同待遇）。

    这条**不需要迁移**：PG 的 ``open_group_key`` 是白名单生成列
    （``state IN ('pending','in_progress')``），escalated 天然被排除。
    """
    store = InMemoryRecordStore()
    r1 = _record("PR-0001")
    await store.write_or_append("default", r1)
    await store.mark_escalated("default", "PR-0001")
    assert await store.find_open("default", r1.group_key) is None

    await store.write_or_append("default", _record("PR-0002"))
    opened = await store.find_open("default", r1.group_key)
    assert opened is not None
    assert opened["record_id"] == "PR-0002"


async def test_mark_escalated_missing_or_wrong_tenant_is_noop() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001", tenant_id="t1"))
    await store.mark_escalated("t2", "PR-0001")
    assert (await store.get("t1", "PR-0001"))["state"] == "pending"
    with pytest.raises(ValueError):
        await store.mark_escalated("", "PR-0001")


async def test_terminal_states_share_one_write_path() -> None:
    """三个终态写的是**同一组列**——抽成一个私有方法就是为了防第三份复制漂移。"""
    store = InMemoryRecordStore()
    for i, (method, state) in enumerate(
        [("resolve", "resolved"), ("close", "closed"), ("mark_escalated", "escalated")]
    ):
        rid = f"PR-100{i}"
        await store.write_or_append("default", _record(rid))
        await getattr(store, method)("default", rid, reason="r")
        row = await store.get("default", rid)
        assert row["state"] == state
        assert row["resolve_reason"] == "r"
        assert isinstance(row["resolved_at"], datetime)


# ---- 超集并入（2026-09-29 加）------------------------------------------------


async def test_superset_group_merges_into_open_record() -> None:
    """超集并入：已开的 {A} 单遇到 {A,B} ⇒ 并进它、不新开，且异常表补上 B。

    实测背景：CPU 风险线与症状线**越线时刻差一轮**，集合因此变大，而 ``group_key``
    是精确集合的哈希 ⇒ 同一场故障被拆成两张单，人要点两次 Analyze。
    """
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001", metrics=["cpu_usage"]))
    second = _record("PR-0002", metrics=["cpu_usage", "cpu_throttled_percent"])
    await store.write_or_append("default", second)

    rows = await store.list("default")
    assert len(rows) == 1, "超集应当并入，不该新开单"
    row = rows[0]
    assert row["record_id"] == "PR-0001", "并入的是**先开**的那条"
    assert {a["metric"] for a in row["metric_anomalies"]} == {"cpu_usage", "cpu_throttled_percent"}
    assert row["occurrence_count"] == 2
    # group_key 提升为超集键 ⇒ 下一轮同样的集合走精确键路径，不再读-改-写
    assert row["group_key"] == second.group_key


async def test_same_superset_again_appends_without_duplicating_anomalies() -> None:
    """并入之后，再来一轮同样的集合：走精确键追加，异常表不重复。"""
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001", metrics=["cpu_usage"]))
    await store.write_or_append("default", _record("PR-0002", metrics=["cpu_usage", "cpu_throttled_percent"]))
    await store.write_or_append("default", _record("PR-0003", metrics=["cpu_usage", "cpu_throttled_percent"]))

    rows = await store.list("default")
    assert len(rows) == 1
    assert len(rows[0]["metric_anomalies"]) == 2
    assert rows[0]["occurrence_count"] == 3


async def test_disjoint_anomaly_sets_still_open_separate_records() -> None:
    """**互不包含**的集合照旧各自成单 —— 超集并入不能变成"按服务一刀切"。"""
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001", metrics=["cpu_usage"]))
    await store.write_or_append("default", _record("PR-0002", metrics=["memory_percent"]))

    rows = await store.list("default")
    assert len(rows) == 2


async def test_subset_does_not_merge_into_superset() -> None:
    """方向是单向的：新的集合**更小**时不并入（那说明另一条线已经恢复，不是同一场）。"""
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001", metrics=["cpu_usage", "cpu_throttled_percent"]))
    await store.write_or_append("default", _record("PR-0002", metrics=["cpu_usage"]))

    rows = await store.list("default")
    assert len(rows) == 2
