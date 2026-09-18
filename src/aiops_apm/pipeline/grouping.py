"""异常分组：把一轮内的 anomaly 划分为若干「事故」组（M9）。

取代原先「按 service 一刀切」的分组（一轮一个 service 只出一条记录）。规则是
**连通分量**：

1. 同 ``signature`` 的日志异常 → 同组；
2. 业务链路 ``trace_ids`` 相交的日志异常 → 同组（**可跨服务**：一次请求失败在
   gateway/order/warranty 各打一条日志，共享 traceId，本质是一个事故）；
3. metric 异常挂到「本服务日志异常」所在的组，从而保住「同源 metric+log 升 critical」。

于是：

===============  ==================================================
场景              结果
===============  ==================================================
3 个 traceId，同一签名    1 条（同一类问题）
1 个 traceId，3 类错误    1 条（同一次请求失败），evidence 列出 3 个签名
不同签名、无共同 trace    多条（各自成单）
traceId 跨 3 个服务       1 条，``service`` 拼接三个服务名
===============  ==================================================

**契约：``group_anomalies`` 是划分** —— 各组成员两两不交，并集恰等于输入。
调用方 ``run_domain`` 依赖这一点：每个异常必须**恰好**进一次 ``l3_verify``。

- 漏掉一个 → ``ctx.seen_keys`` 少一个 key → ``sweep`` 每轮给它 ``miss_rounds + 1``
  → reconciler 判定「全部 anomaly_key 都已消失」→ **自动关掉还活着的单**；
- 一个异常进两组 → ``l3_verify`` 被调两次 → ``consecutive_rounds`` 每轮双增
  → 持续性闸门被绕过，**提前开单**。

两种都是静默的（现有测试不覆盖），所以 ``group_anomalies`` 结尾会断言这个不变量。

纯函数、零 IO、确定性。
"""

from __future__ import annotations

from typing import Any

from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly

__all__ = ["group_anomalies", "group_services", "representative_service"]


class _UnionFind:
    """并查集（路径压缩）。"""

    __slots__ = ("_parent",)

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:  # 路径压缩
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def _union_logs(uf: _UnionFind, logs: list[tuple[int, Any]]) -> None:
    """日志异常的两条连边规则：同 signature、trace_ids 相交。"""
    by_signature: dict[str, int] = {}
    by_trace: dict[str, int] = {}
    for idx, a in logs:
        if a.signature in by_signature:
            uf.union(by_signature[a.signature], idx)
        else:
            by_signature[a.signature] = idx
        for tid in a.trace_ids:
            if tid in by_trace:
                uf.union(by_trace[tid], idx)
            else:
                by_trace[tid] = idx


def group_anomalies(anomalies: list[Any]) -> list[list[Any]]:
    """把 ``anomalies`` 划分为事故组。组按组内最小下标排序，结果确定。"""
    if not anomalies:
        return []

    uf = _UnionFind(len(anomalies))
    logs = [(i, a) for i, a in enumerate(anomalies) if isinstance(a, LogAnomaly)]
    metrics = [(i, a) for i, a in enumerate(anomalies) if isinstance(a, MetricAnomaly)]

    _union_logs(uf, logs)

    # metric 挂到本服务的日志组。**仅当该服务的日志异常同属一个分量时**才并入——
    # 否则「并入全部」会把该服务几个**不同类型**的日志组强行合并成一条记录，
    # 那正是 M9 要避免的（不同类型 error 应各自成单）。这种歧义场景下让 metric
    # 自成一組，日志分组保持独立。
    log_root_by_service: dict[str, set[int]] = {}
    for idx, log in logs:
        log_root_by_service.setdefault(log.service, set()).add(uf.find(idx))
    for idx, metric in metrics:
        roots = log_root_by_service.get(metric.service)
        if roots and len(roots) == 1:
            uf.union(next(iter(roots)), idx)

    # 注意不能用 ``anomalies.index(g[0])`` 排序：pydantic 模型按**值**比较，两个内容
    # 相同的异常会返回同一个下标（且是 O(n²)）。按 root 记下最小下标即可。
    components: dict[int, list[Any]] = {}
    first_index: dict[int, int] = {}
    for idx, a in enumerate(anomalies):
        root = uf.find(idx)
        components.setdefault(root, []).append(a)
        first_index.setdefault(root, idx)

    groups = [components[root] for root in sorted(components, key=lambda r: first_index[r])]

    # 划分不变量（见模块 docstring）：漏掉或重复都会静默破坏 seen_keys 语义。
    assert sum(len(g) for g in groups) == len(anomalies), "分组不是划分：成员数不匹配"
    assert len({id(a) for g in groups for a in g}) == len(anomalies), "分组不是划分：存在重复成员"
    return groups


def group_services(group: list[Any]) -> list[str]:
    """组内服务名（去重、排序）。

    排序是为了确定性：``service`` 列的拼接串、``group_key`` 的代表服务都从这里取，
    跨轮必须稳定——否则去重键每轮都变，会重复开单且 fpr 条目成孤儿。
    """
    return sorted({a.service for a in group})


def representative_service(group: list[Any]) -> str:
    """组的代表服务 = ``group_services`` 的第一个（排序最小）。

    只用于 ``group_key`` 的 service 段（``ProblemRecord.group_key_service``）：
    该段的长度必须可控，否则 ``group_key`` / ``open_group_key`` 生成列 / 唯一索引
    三处都得加宽。对外可见的 ``service`` 列仍是完整拼接串。
    """
    services = group_services(group)
    return services[0] if services else "unknown"
