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


# ---- 卡死轮恢复：上一轮 running 孤儿 → interrupted + 水位线回退 ----


async def test_run_group_recovers_orphan_running_target() -> None:
    storage = await build_storage(_settings())
    try:
        await _seed_targets(storage, _target(interval=0))
        # 模拟上一轮崩溃残留：target 的上一轮 still running，且之前有一轮 ok（结束于 TS_OK）
        TS_OK = T0 - timedelta(seconds=300)
        await storage.rounds.create_round("default", "R-ok", "application", started_at=TS_OK)
        await storage.rounds.create_target("default", "R-ok", "MT-0001", started_at=TS_OK)
        await storage.rounds.update_target_status("default", "R-ok", "MT-0001", "ok", finished_at=TS_OK)
        await storage.rounds.create_round("default", "R-orphan", "application", started_at=T0 - timedelta(seconds=30))
        await storage.rounds.create_target("default", "R-orphan", "MT-0001", started_at=T0 - timedelta(seconds=30))

        clock = FakeClock(T0)
        runner = Recorder()
        sched = Scheduler(
            _settings(), PluginRegistry(), storage,
            now_fn=clock, jitter_fn=lambda i: 0, run_round_fn=runner, holder_id="sched-A",
        )
        assert await sched.tick() == 1
        # 孤儿 target 行被标记 interrupted
        orphan = await storage.rounds.latest_target("default", "MT-0001")
        assert orphan["status"] == "interrupted"
        assert orphan["round_id"] == "R-orphan"
        assert orphan["error"] == "orphan recovery"
        # 孤儿 round 本身（仍 running）也一并标记 interrupted，清掉 round 级孤儿
        orphan_round = await storage.rounds.get_round("default", "R-orphan")
        assert orphan_round["status"] == "interrupted"
        # 水位线回退到上次成功轮次的结束时间
        wm = await storage.watermarks.get("default", "MT-0001")
        assert wm is not None and wm["last_ts"] == TS_OK
    finally:
        await storage.close()


async def test_run_group_recovers_orphan_masked_by_newer_ok_round() -> None:
    storage = await build_storage(_settings())
    try:
        await _seed_targets(storage, _target(interval=0))
        # 崩溃残留孤儿（running），之后一次手动 run 正常结束（ok，started_at 更新）
        await storage.rounds.create_round("default", "R-orphan", "application", started_at=T0 - timedelta(seconds=60))
        await storage.rounds.create_target("default", "R-orphan", "MT-0001", started_at=T0 - timedelta(seconds=60))
        await storage.rounds.create_round("default", "R-manual", "application", started_at=T0 - timedelta(seconds=30))
        await storage.rounds.create_target("default", "R-manual", "MT-0001", started_at=T0 - timedelta(seconds=30))
        await storage.rounds.update_target_status(
            "default", "R-manual", "MT-0001", "ok", finished_at=T0 - timedelta(seconds=29)
        )

        clock = FakeClock(T0)
        runner = Recorder()
        sched = Scheduler(
            _settings(), PluginRegistry(), storage,
            now_fn=clock, jitter_fn=lambda i: 0, run_round_fn=runner, holder_id="sched-A",
        )
        assert await sched.tick() == 1
        # 最新一行是 ok（手动 run），但更早的 running 孤儿仍被找到并标记 interrupted
        orphan = await storage.rounds.latest_target("default", "MT-0001", status="interrupted")
        assert orphan is not None and orphan["round_id"] == "R-orphan"
        orphan_round = await storage.rounds.get_round("default", "R-orphan")
        assert orphan_round["status"] == "interrupted"
    finally:
        await storage.close()


async def test_run_group_ignores_non_running_previous_target() -> None:
    storage = await build_storage(_settings())
    try:
        await _seed_targets(storage, _target(interval=0))
        # 上一轮正常结束（ok）→ 不该被标记 interrupted，也不该回退水位线
        await storage.rounds.create_round("default", "R-ok", "application", started_at=T0 - timedelta(seconds=60))
        await storage.rounds.create_target("default", "R-ok", "MT-0001", started_at=T0 - timedelta(seconds=60))
        await storage.rounds.update_target_status(
            "default", "R-ok", "MT-0001", "ok", finished_at=T0 - timedelta(seconds=59)
        )
        await storage.watermarks.update("default", "MT-0001", T0 - timedelta(seconds=59))

        clock = FakeClock(T0)
        runner = Recorder()
        sched = Scheduler(
            _settings(), PluginRegistry(), storage,
            now_fn=clock, jitter_fn=lambda i: 0, run_round_fn=runner, holder_id="sched-A",
        )
        assert await sched.tick() == 1
        ok = await storage.rounds.latest_target("default", "MT-0001")
        assert ok["status"] == "ok"  # 未被误标 interrupted
        # 上一轮正常结束 → 该 round 不被标 interrupted（保持 running 无孤儿，实际由 update_status 收尾）
        r_ok = await storage.rounds.get_round("default", "R-ok")
        assert r_ok["status"] != "interrupted"
        wm = await storage.watermarks.get("default", "MT-0001")
        assert wm["last_ts"] == T0 - timedelta(seconds=59)  # 水位线未动
    finally:
        await storage.close()


async def test_tick_isolates_crashing_round() -> None:
    """单轮 run_round 崩溃不 kill 调度循环：异常被隔离，target 仍被重排，下个到点继续跑。"""
    storage = await build_storage(_settings())
    try:
        await _seed_targets(storage, _target(interval=0))
        calls = 0

        async def boom(**kw: object) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("collect boom")

        clock = FakeClock(T0)
        sched = Scheduler(
            _settings(), PluginRegistry(), storage,
            now_fn=clock, jitter_fn=lambda i: 0, run_round_fn=boom, holder_id="sched-A",
        )
        assert await sched.tick() == 1  # 崩溃轮正常返回（异常被 _run_group 吞掉）
        assert calls == 1
        clock.set(T0 + timedelta(seconds=60))
        assert await sched.tick() == 1  # 循环没死，继续触发
        assert calls == 2
    finally:
        await storage.close()


async def test_run_group_continues_when_orphan_recovery_fails(monkeypatch) -> None:
    """孤儿恢复失败只告警，不阻断本轮采集（恢复是尽力而为）。"""
    storage = await build_storage(_settings())
    try:
        await _seed_targets(storage, _target(interval=0))
        runner = Recorder()

        async def boom(tenant: str, target_id: str, now: datetime) -> None:
            raise RuntimeError("db down")

        clock = FakeClock(T0)
        sched = Scheduler(
            _settings(), PluginRegistry(), storage,
            now_fn=clock, jitter_fn=lambda i: 0, run_round_fn=runner, holder_id="sched-A",
        )
        monkeypatch.setattr(sched, "_recover_orphan", boom)
        assert await sched.tick() == 1
        assert len(runner.calls) == 1  # 本轮照常采集
    finally:
        await storage.close()
