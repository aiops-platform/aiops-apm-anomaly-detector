"""M9 异常分组：连通分量划分（同 signature 或同 trace_id 归一组，可跨服务）。

``group_anomalies`` 是**划分**——这个不变量是整套设计的基石：每个异常必须恰好进一次
``l3_verify``。漏掉会让 ``sweep`` 误判 miss → reconciler 自动关掉活着的单；重复会让
``consecutive_rounds`` 每轮双增 → 持续性闸门被绕过。所以下面的测试既验分组规则，
也守这个不变量。
"""

from datetime import datetime, timezone

from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly
from aiops_apm.pipeline.grouping import group_anomalies, group_services, representative_service

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def log(*, service="svc-a", sig="OOMError", trace_ids=(), count=7) -> LogAnomaly:
    return LogAnomaly(
        service=service, level="ERROR", signature=sig, pattern="p", count=count,
        first_seen=TS, severity="high", detected_at=TS, trace_ids=list(trace_ids),
    )


def metric(*, service="svc-a", name="cpu_usage") -> MetricAnomaly:
    return MetricAnomaly(
        service=service, metric=name, value=0.95, method="static_threshold",
        severity="high", detected_at=TS,
    )


def _assert_partition(anomalies, groups) -> None:
    """分组必须是划分：并集 == 输入，且两两不交。"""
    flat = [a for g in groups for a in g]
    assert len(flat) == len(anomalies), "成员数不匹配"
    assert len({id(a) for a in flat}) == len(anomalies), "存在重复成员（会双增 consecutive_rounds）"


# ---- 分组规则 ----


def test_empty_input_yields_no_groups() -> None:
    assert group_anomalies([]) == []


def test_single_anomaly_is_one_group() -> None:
    a = log()
    assert group_anomalies([a]) == [[a]]


def test_same_signature_different_traces_merge() -> None:
    """同一签名跨 3 个 traceId → 1 组（同一类问题，不该裂成三条）。"""
    anoms = [log(trace_ids=["t1"]), log(trace_ids=["t2"]), log(trace_ids=["t3"])]
    groups = group_anomalies(anoms)
    assert len(groups) == 1
    assert len(groups[0]) == 3
    _assert_partition(anoms, groups)


def test_shared_trace_id_merges_different_signatures() -> None:
    """1 个 traceId 报了 3 类不同错误 → 1 组（同一次请求失败）。"""
    anoms = [log(sig="ErrA", trace_ids=["t1"]), log(sig="ErrB", trace_ids=["t1"]), log(sig="ErrC", trace_ids=["t1"])]
    groups = group_anomalies(anoms)
    assert len(groups) == 1
    assert {a.signature for a in groups[0]} == {"ErrA", "ErrB", "ErrC"}


def test_different_signatures_no_shared_trace_split() -> None:
    """不同签名、无共同 traceId → 各自成单（M9 的核心诉求）。"""
    anoms = [log(sig="ErrA", trace_ids=["t1"]), log(sig="ErrB", trace_ids=["t2"])]
    groups = group_anomalies(anoms)
    assert len(groups) == 2
    _assert_partition(anoms, groups)


def test_transitive_merge_through_shared_trace() -> None:
    """连通分量是**传递**的：A—(trace t1)—B—(trace t2)—C 归一组。"""
    anoms = [
        log(sig="ErrA", trace_ids=["t1"]),
        log(sig="ErrB", trace_ids=["t1", "t2"]),
        log(sig="ErrC", trace_ids=["t2"]),
    ]
    groups = group_anomalies(anoms)
    assert len(groups) == 1


def test_trace_spans_services_merges_and_joins_service_names() -> None:
    """traceId 跨 3 个服务 → 1 组，service 列表按排序拼接。"""
    anoms = [
        log(service="order-service", sig="ErrA", trace_ids=["t1"]),
        log(service="gateway-service", sig="ErrB", trace_ids=["t1"]),
        log(service="warranty-service", sig="ErrC", trace_ids=["t1"]),
    ]
    groups = group_anomalies(anoms)
    assert len(groups) == 1
    assert group_services(groups[0]) == ["gateway-service", "order-service", "warranty-service"]
    assert representative_service(groups[0]) == "gateway-service"


def test_same_signature_across_services_merges() -> None:
    """同签名跨服务也是同一类问题 → 1 组。"""
    anoms = [log(service="svc-a", sig="Same"), log(service="svc-b", sig="Same")]
    groups = group_anomalies(anoms)
    assert len(groups) == 1
    assert group_services(groups[0]) == ["svc-a", "svc-b"]


# ---- metric 的处理 ----


def test_metric_attaches_to_same_service_log_group() -> None:
    """metric 挂到本服务日志组 → 保住「同源 metric+log 升 critical」。"""
    m, lg = metric(service="svc-a"), log(service="svc-a")
    groups = group_anomalies([m, lg])
    assert len(groups) == 1
    assert set(map(id, groups[0])) == {id(m), id(lg)}


def test_metric_alone_forms_own_group() -> None:
    """没有日志的服务（metric_only）→ 自成一組，保持既有行为。"""
    m = metric(service="svc-a")
    groups = group_anomalies([m])
    assert len(groups) == 1 and groups[0] == [m]


def test_metric_does_not_merge_distinct_log_groups_of_same_service() -> None:
    """该服务有多个日志组时，metric **不得**把它们合并。

    这是 M9 的核心诉求（不同类型 error 各自成单）。「metric 并入该服务全部日志异常」
    那种实现会把 ErrA/ErrB 强行并成一条记录，所以这里让 metric 自成一組。
    """
    m = metric(service="svc-a")
    a = log(service="svc-a", sig="ErrA", trace_ids=["t1"])
    b = log(service="svc-a", sig="ErrB", trace_ids=["t2"])
    groups = group_anomalies([m, a, b])
    assert len(groups) == 3, "metric 不该把两个不同类型的日志组合并"
    _assert_partition([m, a, b], groups)


def test_metric_does_not_attach_across_services() -> None:
    """metric 只挂本服务的日志组，不跨服务。"""
    m = metric(service="svc-a")
    lg = log(service="svc-b", sig="ErrA")
    groups = group_anomalies([m, lg])
    assert len(groups) == 2
    assert group_services(groups[0]) == ["svc-a"]
    assert group_services(groups[1]) == ["svc-b"]


# ---- 划分不变量 ----


def test_partition_invariant_holds_for_mixed_batch() -> None:
    """混合批次：所有异常恰好出现一次，无遗漏无重复。"""
    m_a = metric(service="svc-a")  # svc-a 有 2 个日志组 → 自成一組
    m_c = metric(service="svc-c")  # svc-c 只有 1 个日志组 → 并入
    anoms = [
        log(service="svc-a", sig="ErrA", trace_ids=["t1"]),
        log(service="svc-b", sig="ErrA", trace_ids=["t1"]),  # 同 trace 跨服务
        log(service="svc-a", sig="ErrB", trace_ids=["t2"]),  # 独立
        log(service="svc-c", sig="ErrC"),                    # 独立
        m_a,
        m_c,
    ]
    groups = group_anomalies(anoms)
    _assert_partition(anoms, groups)
    # svc-c 的 metric 应与该服务的日志并组（用 is 比对，避免 pydantic 按值相等误命中）
    grp_c = next(g for g in groups if any(x is m_c for x in g))
    assert any(isinstance(x, LogAnomaly) and x.service == "svc-c" for x in grp_c)
    # svc-a 有 2 个日志组 → metric 不与任何一个合并
    grp_a = next(g for g in groups if any(x is m_a for x in g))
    assert len(grp_a) == 1


def test_group_order_is_deterministic() -> None:
    """组按组内最小下标排序，结果可预期（便于比对与调试）。"""
    a, b = log(sig="ErrA", trace_ids=["t1"]), log(sig="ErrB", trace_ids=["t2"])
    assert group_anomalies([a, b])[0][0] is a
    assert group_anomalies([b, a])[0][0] is b


def test_representative_service_stable_across_rounds() -> None:
    """代表服务必须跨轮稳定 —— 它进 group_key，不稳会让去重失效、重复开单。"""
    r1 = group_anomalies([
        log(service="order-service", sig="ErrA", trace_ids=["t1"]),
        log(service="gateway-service", sig="ErrB", trace_ids=["t1"]),
    ])[0]
    # 第二轮同样的两个服务，但日志顺序/条数不同
    r2 = group_anomalies([
        log(service="gateway-service", sig="ErrB", trace_ids=["t1"]),
        log(service="order-service", sig="ErrA", trace_ids=["t1"]),
    ])[0]
    assert representative_service(r1) == representative_service(r2) == "gateway-service"
