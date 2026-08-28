"""Scheduler：按 ``monitor_target.schedule`` 自动触发一轮检测（M6 UC-6.1/6.9）。

- ``tick()`` 单步可测：注入 ``now_fn``/``jitter_fn``/``run_round_fn``，不真实 sleep。
- 多副本单调度器（UC-6.9）：每个 tick 先抢 ``scheduler`` lease，抢不到即跳过；
  抢到则干活并在 tick 末尾续约。``scheduler_tick_sec(1s) << lease ttl(30s)`` 保证持约方稳定。
- 目标调度：``_next_run[(tenant, target_id)]`` 首次观测初始化为 ``now + interval``（避免启动即全量风暴），
  到点（``<= now``）触发，触发后重排为 ``now + interval + jitter``。
- 并发闸门：``max_concurrent_rounds`` 信号量（惰性创建，避免绑定事件循环）；
  ``(tenant, domain)`` 组内 in-flight 去重。
- 卡死轮恢复（V5）：每轮开跑前查该 target 上一轮 ``detection_round_target`` 状态；
  若仍 running（进程崩溃残留的孤儿）→ 标记 interrupted 并把水位线回退到上次
  成功轮次结束时间 —— 新轮从上次成功结束处续采，不丢数据。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from aiops_apm.audit import SecurityAudit
from aiops_apm.poller import run_round
from aiops_apm.storage import Storage

logger = logging.getLogger(__name__)

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

    async def _recover_orphan(self, tenant: str, target_id: str, now: datetime) -> None:
        """该 target 上一轮仍 running（进程崩溃残留）→ 标记 interrupted + 水位线回退。

        新轮从上次成功轮次结束时间续采：``collect_watermark.last_ts`` 回退到最近
        ``status='ok'`` 的 ``finished_at``（采集器按 signature 幂等去重，重复无害、不丢数据）。
        孤儿轮本身若仍 running（整轮崩溃残留）→ 一并标记 interrupted，清掉 round 级孤儿。
        """
        # 按「最近一条仍 running 的明细」定位孤儿：即使最新一轮已正常结束（如手动 run 覆盖），
        # 更早崩溃残留的 running 行也会被找到并清掉（M1：防孤儿被更新的正常行遮蔽）。
        orphan = await self._storage.rounds.latest_target(tenant, target_id, status="running")
        if orphan is None:
            return
        await self._storage.rounds.update_target_status(
            tenant,
            orphan["round_id"],
            target_id,
            "interrupted",
            finished_at=now,
            error="orphan recovery",
        )
        round_row = await self._storage.rounds.get_round(tenant, orphan["round_id"])
        if round_row is not None and round_row["status"] == "running":
            await self._storage.rounds.update_status(tenant, orphan["round_id"], "interrupted", ended_at=now)
        ok = await self._storage.rounds.latest_target(tenant, target_id, status="ok")
        if ok is not None and ok["finished_at"] is not None:
            await self._storage.watermarks.update(tenant, target_id, ok["finished_at"])

    async def _run_group(
        self, sem: asyncio.Semaphore, key: tuple[str, str], targets: list, now: datetime
    ) -> None:
        tenant, domain = key
        async with sem:
            if key in self._in_flight:
                return
            self._in_flight.add(key)
            try:
                # 开跑前清孤儿：上一轮 running → interrupted + 水位线回退（若崩溃在采集途中）。
                # 孤儿恢复是尽力而为：失败只告警，不阻断本轮采集。
                for t in targets:
                    try:
                        await self._recover_orphan(tenant, str(t["target_id"]), now)
                    except Exception as exc:  # noqa: BLE001 -- 恢复失败不影响本轮
                        logger.warning(
                            "orphan recovery failed tenant=%s target=%s: %s",
                            tenant, t["target_id"], exc,
                        )
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
            except Exception as exc:  # noqa: BLE001 -- 单轮异常隔离：记录后继续调度，不 kill 循环
                logger.error(
                    "round failed tenant=%s domain=%s targets=%s: %s: %s",
                    tenant, domain,
                    ",".join(str(t.get("target_id", "unknown")) for t in targets),
                    type(exc).__name__, exc,
                )
                SecurityAudit.log_round_event(
                    tenant, "", domain, "failed",
                    detail=f"run_round {type(exc).__name__}: {exc}",
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
