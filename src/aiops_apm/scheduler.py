"""Scheduler：按 ``monitor_target.schedule`` 自动触发一轮检测（M6 UC-6.1/6.9）。

- ``tick()`` 单步可测：注入 ``now_fn``/``jitter_fn``/``run_round_fn``，不真实 sleep。
- 多副本单调度器（UC-6.9）：每个 tick 先抢 ``scheduler`` lease，抢不到即跳过；
  抢到则干活并在 tick 末尾续约。``scheduler_tick_sec(1s) << lease ttl(30s)`` 保证持约方稳定。
- 目标调度：``_next_run[(tenant, target_id)]`` 首次观测初始化为 ``now + interval``（避免启动即全量风暴），
  到点（``<= now``）触发，触发后重排为 ``now + interval + jitter``。
- 并发闸门：``max_concurrent_rounds`` 信号量（惰性创建，避免绑定事件循环）；
  ``(tenant, domain)`` 组内 in-flight 去重。
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from aiops_apm.poller import run_round
from aiops_apm.storage import Storage

RoundRunner = Callable[..., Any]


class Scheduler:
    def __init__(
        self,
        settings: Any,
        registry: Any,
        storage: Storage,
        *,
        http: Any = None,
        now_fn: Callable[[], datetime] | None = None,
        jitter_fn: Callable[[float], float] | None = None,
        run_round_fn: RoundRunner | None = None,
        holder_id: str | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._storage = storage
        self._http = http
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._jitter_fn = jitter_fn or (
            lambda interval: interval * float(settings.scheduler_jitter_ratio)
        )
        self._run_round_fn = run_round_fn or run_round
        self._holder = holder_id or f"scheduler-{uuid.uuid4().hex[:8]}"
        # (tenant_id, target_id) -> 下次应跑时间
        self._next_run: dict[tuple[str, str], datetime] = {}
        self._in_flight: set[tuple[str, str]] = set()
        self._semaphore: asyncio.Semaphore | None = None
        self._stop = asyncio.Event()

    def _sem(self) -> asyncio.Semaphore:
        # 惰性创建：asyncio.Semaphore 构造时绑定事件循环，pytest-asyncio 每用例新 loop。
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._settings.max_concurrent_rounds)
        return self._semaphore

    async def _due_targets(self, now: datetime) -> list[tuple[str, dict]]:
        due: list[tuple[str, dict]] = []
        for tenant in await self._storage.monitor_targets.list_tenants():
            for t in await self._storage.monitor_targets.load_all_targets(tenant):
                interval = float(t.get("schedule", {}).get("interval_sec", 60))
                key = (tenant, t["target_id"])
                if key not in self._next_run:
                    self._next_run[key] = now + timedelta(seconds=interval)
                # interval=0 → 首次观测即到点（now+0 <= now）
                if self._next_run[key] <= now:
                    due.append((tenant, t))
        return due

    async def tick(self) -> int:
        """单步调度：抢 lease → 找 due 目标 → 按 (tenant, domain) 组并行跑一轮 → 续约。

        返回本轮跑了几组；未抢到 lease 返回 0。
        """
        leases = self._storage.leases
        if not await leases.try_acquire("scheduler", self._holder, self._settings.scheduler_lease_ttl_sec):
            return 0
        now = self._now_fn()
        due = await self._due_targets(now)

        groups: dict[tuple[str, str], list] = defaultdict(list)
        for tenant, t in due:
            key = (tenant, str(t.get("domain", "application")))
            if key in self._in_flight:
                continue
            groups[key].append(t)

        rounds = 0
        if groups:
            sem = self._sem()
            round_tasks = [
                self._run_group(sem, key, targets, now) for key, targets in groups.items()
            ]
            await asyncio.gather(*round_tasks)
            rounds = len(groups)
        await leases.renew("scheduler", self._holder, self._settings.scheduler_lease_ttl_sec)
        return rounds

    async def _run_group(
        self, sem: asyncio.Semaphore, key: tuple[str, str], targets: list, now: datetime
    ) -> None:
        tenant, domain = key
        async with sem:
            if key in self._in_flight:
                return
            self._in_flight.add(key)
            try:
                await self._run_round_fn(
                    registry=self._registry,
                    storage=self._storage,
                    tenant_id=tenant,
                    domain=domain,
                    targets=targets,
                    now=now,
                    http=self._http,
                    settings=self._settings,
                )
            finally:
                self._in_flight.discard(key)
                for t in targets:
                    interval = float(t.get("schedule", {}).get("interval_sec", 60))
                    self._next_run[(tenant, t["target_id"])] = now + timedelta(
                        seconds=interval + self._jitter_fn(interval)
                    )

    async def run(self) -> None:
        """后台循环：每 ``scheduler_tick_sec`` tick 一次，直到 ``stop()``。"""
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._settings.scheduler_tick_sec)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
