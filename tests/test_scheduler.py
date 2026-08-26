"""M6 UC-6.1/6.9：Scheduler tick 单步（注入时钟/jitter/执行器，不真实 sleep）。"""

from datetime import datetime, timedelta, timezone

from aiops_apm.pipeline.context import DomainResult
from aiops_apm.plugins.registry import PluginRegistry
from aiops_apm.scheduler import Scheduler
from aiops_apm.settings import Settings
from aiops_apm.storage import Storage, build_storage

T0 = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def set(self, dt: datetime) -> None:
        self._now = dt

    def __call__(self) -> datetime:
        return self._now


def _settings(**over) -> Settings:
    base = dict(
        _env_file=None,
        storage_backend="memory",
        enable_scheduler=False,
        scheduler_tick_sec=1.0,
        scheduler_lease_ttl_sec=30.0,
        scheduler_jitter_ratio=0.1,
        max_concurrent_rounds=10,
    )
    base.update(over)
    return Settings(**base)


async def _seed_targets(storage: Storage, *targets: dict) -> None:
    for t in targets:
        await storage.monitor_targets.create("default", t)


def _target(*, service="svc-a", domain="application", interval=60) -> dict:
    return {
        "service": service,
        "signal_type": "metric",
        "source_type": "mock",
        "domain": domain,
        "source_config": {},
        "schedule": {"interval_sec": interval},
        "enabled": True,
    }


class Recorder:
    """记录 run_round 调用，返回空 DomainResult。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kw) -> DomainResult:
        self.calls.append(kw)
        return DomainResult(
            domain=kw["domain"], records=[], suppressed_count=0, anomaly_count=0,
            degraded_sources=[], timeline=[],
        )


async def test_tick_runs_when_due_and_skips_otherwise() -> None:
    storage = await build_storage(_settings())
    try:
        await _seed_targets(storage, _target(interval=60))
        clock = FakeClock(T0)
        runner = Recorder()
        sched = Scheduler(
            _settings(), PluginRegistry(), storage,
            now_fn=clock, jitter_fn=lambda i: 0, run_round_fn=runner, holder_id="sched-A",
        )
        # 首次 tick：初始化 next_run = T0+60 → 未到点
        assert await sched.tick() == 0
        assert runner.calls == []

        # 到点 T0+60 → 触发
        clock.set(T0 + timedelta(seconds=60))
        assert await sched.tick() == 1
        assert len(runner.calls) == 1
        assert runner.calls[0]["tenant_id"] == "default"
        assert runner.calls[0]["domain"] == "application"
        assert runner.calls[0]["targets"][0]["target_id"] == "MT-0001"

        # 未到点（下一轮 T0+120）→ 跳过
        clock.set(T0 + timedelta(seconds=90))
        assert await sched.tick() == 0
        assert len(runner.calls) == 1
    finally:
        await storage.close()


async def test_tick_skips_when_lease_held_by_other() -> None:
    storage = await build_storage(_settings())
    try:
        await _seed_targets(storage, _target(interval=0))  # interval=0 → 首次即到点
        clock = FakeClock(T0)
        await storage.leases.try_acquire("scheduler", "other-replica", 30)
        runner = Recorder()
        sched = Scheduler(
            _settings(), PluginRegistry(), storage,
            now_fn=clock, jitter_fn=lambda i: 0, run_round_fn=runner, holder_id="sched-A",
        )
        assert await sched.tick() == 0
        assert runner.calls == []
    finally:
        await storage.close()


async def test_tick_groups_targets_by_tenant_domain() -> None:
    storage = await build_storage(_settings())
    try:
        await _seed_targets(
            storage,
            _target(service="svc-a", domain="application", interval=0),
            _target(service="svc-b", domain="application", interval=0),
            _target(service="svc-c", domain="infra", interval=0),
        )
        clock = FakeClock(T0)
        runner = Recorder()
        sched = Scheduler(
            _settings(), PluginRegistry(), storage,
            now_fn=clock, jitter_fn=lambda i: 0, run_round_fn=runner, holder_id="sched-A",
        )
        assert await sched.tick() == 2  # 两个 (tenant, domain) 组
        app_call = next(c for c in runner.calls if c["domain"] == "application")
        assert {t["service"] for t in app_call["targets"]} == {"svc-a", "svc-b"}
        infra_call = next(c for c in runner.calls if c["domain"] == "infra")
        assert len(infra_call["targets"]) == 1
    finally:
        await storage.close()


async def test_tick_skips_in_flight_group() -> None:
    storage = await build_storage(_settings())
    try:
        await _seed_targets(storage, _target(interval=0))
        clock = FakeClock(T0)
        runner = Recorder()
        sched = Scheduler(
            _settings(), PluginRegistry(), storage,
            now_fn=clock, jitter_fn=lambda i: 0, run_round_fn=runner, holder_id="sched-A",
        )
        # 人为标记该组 in-flight → tick 应跳过
        sched._in_flight.add(("default", "application"))
        assert await sched.tick() == 0
        assert runner.calls == []
        sched._in_flight.discard(("default", "application"))
    finally:
        await storage.close()
