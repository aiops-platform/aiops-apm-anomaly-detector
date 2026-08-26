"""Reconciler：自动关闭长期 miss 的 pending 单（M6 UC-6.7）。

周期性扫描 open 记录：重建每单的 anomaly_keys（从 ``metric_anomalies``/``log_anomalies``
JSON dict 反序列化回模型 → ``fingerprint.anomaly_key``），与 ``detection_state`` 比对——
全部 key 的 ``miss_rounds >= resolve_after_rounds`` 才 ``resolve(reason="auto")``。
部分 key 仍活跃则不关（避免误关未恢复问题）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiops_apm.models import fingerprint
from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly
from aiops_apm.storage import Storage

_OPEN_STATES = ("pending", "in_progress")


def record_anomalies(rec: dict) -> list:
    """从记录 JSON 重建该单的 anomaly 对象列表（与 L3 用同一真源，保证可比对）。

    M7（UC-7.6 fpr 回写）由 ``problems`` 复用，重建 group_key 用。
    """
    anomalies: list = []
    for d in rec.get("metric_anomalies") or []:
        anomalies.append(MetricAnomaly.model_validate(d))
    for d in rec.get("log_anomalies") or []:
        anomalies.append(LogAnomaly.model_validate(d))
    return anomalies


def record_anomaly_keys(rec: dict) -> list[str]:
    """从记录 JSON 重建该单的 anomaly_keys（与 L3 用同一真源，保证可比对）。"""
    return [fingerprint.anomaly_key(a) for a in record_anomalies(rec)]


class Reconciler:
    def __init__(
        self,
        settings: Any,
        storage: Storage,
        *,
        now_fn: Any = None,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._now_fn = now_fn or (lambda: None)
        self._stop = asyncio.Event()

    async def reconcile_once(self) -> int:
        """扫一遍 open 记录，返回自动关闭的单数。"""
        records = self._storage.records
        states = self._storage.detection_state
        resolved = 0
        for tenant in await records.list_tenants():
            open_recs = [r for r in await records.list(tenant, limit=10_000) if r["state"] in _OPEN_STATES]
            for rec in open_recs:
                keys = record_anomaly_keys(rec)
                if not keys:
                    continue
                by_domain = await states.list_by_domain(tenant, rec["domain"])
                stale = all(
                    by_domain.get(k, {}).get("miss_rounds", 0) >= self._settings.resolve_after_rounds
                    for k in keys
                )
                if stale:
                    await records.resolve(tenant, rec["record_id"], reason="auto")
                    resolved += 1
        return resolved

    async def run(self) -> None:
        """后台循环：每 ``resolve_check_interval_sec`` 扫一次，直到 ``stop()``。"""
        while not self._stop.is_set():
            await self.reconcile_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._settings.resolve_check_interval_sec)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
