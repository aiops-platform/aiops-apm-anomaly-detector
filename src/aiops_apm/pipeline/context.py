"""检测上下文：``DetectionContext`` + ``DomainResult`` + ``build_context`` 工厂。

一个 ``(tenant_id, domain)`` 内一轮检测的完整上下文：规则（domain_config）、插件注册表、
存储、动态配置（维护窗口/黑名单/fpr/变更）与本轮数据（signals/suppressed/anomalies）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from aiops_apm.config.loader import DomainConfigLoader
from aiops_apm.models.config import DomainConfig
from aiops_apm.models.signal import ChangeSignal
from aiops_apm.plugins.registry import PluginRegistry
from aiops_apm.storage import Storage
from aiops_apm.storage.detection_state import DetectionStateStore
from aiops_apm.storage.records import RecordStore
from aiops_apm.storage.sequence import SequenceStore
from aiops_apm.storage.snapshots import SnapshotStore
from aiops_apm.storage.watermarks import WatermarkStore


def new_trace_id() -> str:
    return f"trace-{uuid.uuid4().hex[:24]}"


@dataclass(kw_only=True)
class DetectionContext:
    """一轮检测的上下文（单一 ``trace_id`` 贯穿，设计原则 #3）。"""

    trace_id: str = field(default_factory=new_trace_id)
    tenant_id: str = "default"
    domain: str
    domain_config: DomainConfig
    registry: PluginRegistry
    storage: RecordStore
    state_store: DetectionStateStore
    sequence_store: SequenceStore
    now: datetime
    # M6 采集/摘要注入（可选，采集器 duck-type 只用 tenant_id/watermark_store/snapshot_store）
    watermark_store: WatermarkStore | None = None
    snapshot_store: SnapshotStore | None = None
    summary_provider: object | None = None  # SummaryProvider；None → emit 走模板
    # 本轮数据
    targets: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    changes: list = field(default_factory=list)  # ChangeSignal 列表（L2 变更关联）
    suppressed: list = field(default_factory=list)  # [{"signal", "reason", "suppressor"}]
    anomalies: list = field(default_factory=list)
    # 动态配置（build_context 从表载入）
    maintenance_windows: list = field(default_factory=list)
    blacklist: list = field(default_factory=list)
    fpr: dict = field(default_factory=dict)  # {group_key: {"fpr": float, "total": int}}
    # 轮次状态
    degraded_sources: list = field(default_factory=list)
    round_started_at: datetime | None = None
    seen_keys: set = field(default_factory=set)  # L3 记本轮到到的 anomaly_key，run_domain sweep 用


@dataclass
class DomainResult:
    """``run_domain`` 单轮产出。"""

    domain: str
    records: list
    suppressed_count: int
    anomaly_count: int
    degraded_sources: list
    timeline: list


async def build_context(
    *,
    tenant_id: str,
    domain: str,
    registry: PluginRegistry,
    storage: Storage,
    now: datetime,
    trace_id: str | None = None,
    signals: list | None = None,
    changes: list | None = None,
    degraded_sources: list | None = None,
    domain_config: DomainConfig | None = None,
    summary_provider: object | None = None,
) -> DetectionContext:
    """载入 domain_config（DomainConfigLoader）+ 四类动态配置（storage.dynamic_config）+ 注入 state/sequence store。"""
    if domain_config is None:
        rows = await DomainConfigLoader(storage.domain_configs).load(tenant_id)
        domain_config = next((DomainConfig.model_validate(r["config"]) for r in rows if r["domain"] == domain), None)
        if domain_config is None:
            raise ValueError(f"no domain config for domain={domain!r} tenant={tenant_id!r}")

    dc_store = storage.dynamic_config
    maintenance_windows = await dc_store.load_maintenance_windows(tenant_id)
    blacklist = await dc_store.load_blacklist(tenant_id)
    fpr = await dc_store.load_fpr(tenant_id)
    if changes is None:
        raw_changes = await dc_store.load_changes(tenant_id)
        changes = [
            ChangeSignal(
                tenant_id=tenant_id, service=c["service"], change_id=c["change_id"],
                type=c["type"], summary=c["summary"], timestamp=c["changed_at"],
            )
            for c in raw_changes
        ]

    return DetectionContext(
        trace_id=trace_id or new_trace_id(),
        tenant_id=tenant_id,
        domain=domain,
        domain_config=domain_config,
        registry=registry,
        storage=storage.records,
        state_store=storage.detection_state,
        sequence_store=storage.sequence,
        now=now,
        watermark_store=storage.watermarks,
        snapshot_store=storage.snapshots,
        summary_provider=summary_provider,
        signals=list(signals or []),
        changes=list(changes or []),
        degraded_sources=list(degraded_sources or []),
        maintenance_windows=maintenance_windows,
        blacklist=blacklist,
        fpr=fpr,
    )
