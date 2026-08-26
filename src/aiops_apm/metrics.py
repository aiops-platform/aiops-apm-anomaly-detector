"""Prometheus 指标（UC-7.1）：检测轮次 / 产出 / 降级 / 抑制 / 误报率 / 耗时。

指标定义在模块级（prometheus_client 默认全局注册表），``/metrics`` 端点
（``_app.py``）用 ``generate_latest()`` 暴露。

**caveat**：``records_created`` 按 ``len(result.records)`` 计数，忽略
``write_or_append`` 的去重（已存在 open 记录只追加不新增行）→ 高估上限值，
仅用于趋势观察，不用于精确计数。
"""

from __future__ import annotations

from typing import Any

from prometheus_client import Counter, Gauge, Histogram

ROUND_TOTAL = Counter(
    "aiops_round_total",
    "检测轮次数（按状态）",
    ["domain", "tenant_id", "status"],
)
ROUND_SUCCESS = Counter(
    "aiops_round_success",
    "成功轮次数",
    ["domain", "tenant_id"],
)
RECORDS_CREATED = Counter(
    "aiops_records_created",
    "单轮产出的 problem_record 数（含去重高估，见模块 docstring）",
    ["service", "severity"],
)
DEGRADED_SOURCES = Counter(
    "aiops_degraded_sources",
    "降级源（采集异常 target）计数",
    ["tenant_id"],
)
SUPPRESSED_TOTAL = Counter(
    "aiops_suppressed_total",
    "被抑制信号数（按 service/suppressor）",
    ["service", "suppressor"],
)
FALSE_POSITIVE_RATE = Gauge(
    "aiops_false_positive_rate",
    "当前误报率（按 service，fpr_table 求均值）",
    ["service"],
)
# 轮次耗时观测桶（秒），覆盖秒级到分钟级调度
_ROUND_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

ROUND_DURATION = Histogram(
    "aiops_round_duration_seconds",
    "单轮检测耗时",
    ["domain", "tenant_id"],
    buckets=_ROUND_BUCKETS,
)


def record_round_metrics(
    *,
    domain: str,
    tenant_id: str,
    status: str,
    duration_sec: float,
    result: Any | None = None,
) -> None:
    """一轮收尾后打点：轮次计数 / 耗时 / 产出 / 降级 / 抑制。"""
    ROUND_TOTAL.labels(domain, tenant_id, status).inc()
    if status == "success":
        ROUND_SUCCESS.labels(domain, tenant_id).inc()
    ROUND_DURATION.labels(domain, tenant_id).observe(duration_sec)

    if result is None:
        return
    for rec in result.records:
        RECORDS_CREATED.labels(rec.service, rec.severity).inc()
    for _s in result.degraded_sources:
        DEGRADED_SOURCES.labels(tenant_id).inc()
    # 从 timeline suppressed 步骤的 details 摊平（service + suppressor 均在 detail 内）
    for step in result.timeline:
        if step.get("step") == "suppressed":
            for d in step.get("details", []):
                SUPPRESSED_TOTAL.labels(d.get("service", "unknown"), d.get("suppressor", "unknown")).inc()


def update_fpr_gauge(tenant_id: str, domain: str, service: str, fpr_data: dict) -> None:
    """按 group_key 前缀 ``{tenant}:{domain}:{service}:`` 求误报率均值写 Gauge。"""
    prefix = f"{tenant_id}:{domain}:{service}:"
    values = [v["fpr"] for k, v in fpr_data.items() if k.startswith(prefix) and v.get("total", 0) > 0]
    avg = (sum(values) / len(values)) if values else 0.0
    FALSE_POSITIVE_RATE.labels(service).set(avg)
