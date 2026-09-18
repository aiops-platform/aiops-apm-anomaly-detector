"""§13 / UC-5.x 端到端：``run_domain`` + ``build_context``（真实 registry entry_points + InMemoryStorage）。

完成标准（Enhanced plan M5）：§13 用例 1/3/4/5/6/7/8/9/10/11 全部通过。
用例 2（内存泄漏组合 → critical）端到端留 M6。
"""

from datetime import datetime, timedelta, timezone

from aiops_apm.models import fingerprint
from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.config import CorrelationSpec, DetectorSpec, DomainConfig, SuppressorSpec, VerifySpec
from aiops_apm.models.signal import ChangeSignal, LogSignal, MetricSignal
from aiops_apm.pipeline.context import build_context
from aiops_apm.pipeline.runner import run_domain
from aiops_apm.plugins.registry import PluginRegistry
from aiops_apm.settings import Settings
from aiops_apm.storage import Storage, build_storage

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


async def make_storage() -> Storage:
    settings = Settings(_env_file=None, storage_backend="memory")
    return await build_storage(settings)


def metric_signal(*, service="svc-a", metric="cpu_usage", value=0.95, ts=TS) -> MetricSignal:
    return MetricSignal(service=service, metric=metric, value=value, timestamp=ts)


def log_signal(
    *, service="svc-a", level="ERROR", message="boom", signature="java.lang.OOMError", ts=TS, trace_id=None
) -> LogSignal:
    return LogSignal(service=service, level=level, message=message, signature=signature, timestamp=ts, trace_id=trace_id)


def domain_with(
    detectors: list[DetectorSpec],
    *,
    suppressors: list[SuppressorSpec] | None = None,
    verify: VerifySpec | None = None,
    correlation: CorrelationSpec | None = None,
) -> DomainConfig:
    return DomainConfig(
        detectors=detectors,
        suppressors=suppressors or [],
        correlation=correlation or CorrelationSpec(),
        verify=verify or VerifySpec(persistence_rounds=1),  # 单轮场景
    )


CPU_DOMAIN = DomainConfig(
    detectors=[DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high")],
    verify=VerifySpec(persistence_rounds=2, false_positive_threshold=0.6, min_samples=20),
)


def cpu_key() -> str:
    return MetricAnomaly(
        service="svc-a", metric="cpu_usage", value=0.9, method="static_threshold", severity="high", detected_at=TS
    ).anomaly_key()


# --- UC-5.1：CPU 飙高两轮（第一轮不开单，第二轮 1 条 high） ---


async def test_uc51_cpu_spike_two_rounds() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        ctx1 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.95)], domain_config=CPU_DOMAIN,
        )
        r1 = await run_domain(ctx1)
        assert r1.records == []
        assert r1.anomaly_count == 1

        ctx2 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage,
            now=TS + timedelta(seconds=60), signals=[metric_signal(value=0.96)], domain_config=CPU_DOMAIN,
        )
        r2 = await run_domain(ctx2)
        assert len(r2.records) == 1
        rec = r2.records[0]
        assert rec.severity == "high"
        assert rec.service == "svc-a"
        assert rec.state == "pending"
        assert rec.correlation.reason == "metric_only"
    finally:
        await storage.close()


# --- UC-5.3：47 条 OOM 日志聚合 → 1 条 anomaly count=47，纯日志开单 ---


async def test_uc53_47_oom_logs_aggregate() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="ERROR", plugin="signature_aggregate", params={"min_count": 5}, severity="warning")]
        )
        signals = [log_signal() for _ in range(47)]
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=signals, domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1
        rec = result.records[0]
        assert len(rec.log_anomalies) == 1
        assert rec.log_anomalies[0].count == 47
        assert rec.metric_anomalies == []
        assert rec.correlation.related is False
        assert rec.correlation.reason == "log_only"
    finally:
        await storage.close()


# --- UC-5.4：指标 + 日志同源关联 → 只有 1 条 record，related=true ---


async def test_uc54_metric_log_same_source() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [
                DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high"),
                DetectorSpec(signal="ERROR", plugin="signature_aggregate", params={"min_count": 1}, severity="warning"),
            ]
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.95, ts=TS), log_signal(ts=TS)], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1  # 只有 1 条（去重/同源关联）
        rec = result.records[0]
        assert rec.correlation.related is True
        assert rec.correlation.reason == "metric_log_within_window"
        assert len(rec.metric_anomalies) == 1
        assert len(rec.log_anomalies) == 1
    finally:
        await storage.close()


# --- UC-5.5：错误率突增 + 部署变更 → change_related=true，recent_change 含 id+summary ---


async def test_uc55_error_rate_change_related() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [
                DetectorSpec(
                    signal="error_rate", plugin="simple_compare",
                    params={"baseline": 0.02, "ratio": 1.5}, severity="high",
                )
            ]
        )
        changes = [ChangeSignal(service="svc-a", change_id="C-100", type="deployment", summary="v2 deploy", timestamp=TS)]
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(metric="error_rate", value=0.1, ts=TS)], changes=changes, domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.change_related is True
        assert rec.recent_change is not None
        assert rec.recent_change["change_id"] == "C-100"
        assert "v2 deploy" in rec.recent_change["summary"]
    finally:
        await storage.close()


# --- UC-5.6：瞬时抖动过滤（三轮不开单，detection_state 反映 cumulative/miss） ---


async def test_uc56_transient_spike_filtered() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        # 第 1 轮 spike 出现（累计 1 次），第 2/3 轮消失 → 累计 1 < persistence_rounds=2，始终不开单
        ctx1 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.98, ts=TS)], domain_config=CPU_DOMAIN,
        )
        r1 = await run_domain(ctx1)
        assert r1.records == []

        ctx2 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage,
            now=TS + timedelta(seconds=60), signals=[metric_signal(value=0.5, ts=TS + timedelta(seconds=60))],
            domain_config=CPU_DOMAIN,
        )
        r2 = await run_domain(ctx2)
        assert r2.records == []

        ctx3 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage,
            now=TS + timedelta(seconds=120), signals=[metric_signal(value=0.5, ts=TS + timedelta(seconds=120))],
            domain_config=CPU_DOMAIN,
        )
        r3 = await run_domain(ctx3)
        assert r3.records == []

        state = await storage.detection_state.get("default", "application", cpu_key())
        assert state is not None
        assert state["consecutive_rounds"] == 1  # 累计 1 次出现，断轮（miss）不清零
        assert state["miss_rounds"] == 2
    finally:
        await storage.close()


# --- 持续性累计语义：断一轮仍算（第 1 轮出现 → 第 2 轮断 → 第 3 轮再出现 → 累计 2 次开单） ---


async def test_persistence_cumulative_gap_then_reappear_opens() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        # 第 1 轮：cpu 飙高 → 累计 1 次，未到 persistence_rounds=2 → 不开单
        ctx1 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.98, ts=TS)], domain_config=CPU_DOMAIN,
        )
        r1 = await run_domain(ctx1)
        assert r1.records == []

        # 第 2 轮：cpu 正常 → 无异常，sweep 记 miss（累计不清零）
        ctx2 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage,
            now=TS + timedelta(seconds=60), signals=[metric_signal(value=0.5, ts=TS + timedelta(seconds=60))],
            domain_config=CPU_DOMAIN,
        )
        r2 = await run_domain(ctx2)
        assert r2.records == []

        # 第 3 轮：cpu 再次飙高 → 累计 2 次 ≥ 2 → 开单（断一轮不打断持续性）
        ctx3 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage,
            now=TS + timedelta(seconds=120), signals=[metric_signal(value=0.97, ts=TS + timedelta(seconds=120))],
            domain_config=CPU_DOMAIN,
        )
        r3 = await run_domain(ctx3)
        assert len(r3.records) == 1
        rec = r3.records[0]
        assert rec.severity == "high"
        assert rec.service == "svc-a"

        state = await storage.detection_state.get("default", "application", cpu_key())
        assert state is not None
        assert state["consecutive_rounds"] == 2  # 累计 2 次出现：断的那一轮没清零
        # 第 3 轮再次出现 → l3_verify 出现时重置 miss_rounds=0（reconcile 防误关）
        assert state["miss_rounds"] == 0
    finally:
        await storage.close()


# --- UC-5.7：维护窗口抑制（不开单，suppressed_count=1，有审计） ---


async def test_uc57_maintenance_window_suppressed() -> None:
    storage = await make_storage()
    try:
        storage.dynamic_config.seed_maintenance_windows(
            "default",
            [
                {
                    "service": "svc-a",
                    "start_at": TS - timedelta(seconds=60),
                    "end_at": TS + timedelta(seconds=60),
                    "reason": "scheduled release",
                }
            ],
        )
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high")],
            suppressors=[SuppressorSpec(name="maintenance_window")],
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.98, ts=TS)], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert result.records == []
        assert result.suppressed_count == 1
        assert any(t["step"] == "suppressed" and t["count"] == 1 for t in result.timeline)
    finally:
        await storage.close()


# --- UC-5.8：误报率闸门（仍开单不永久静默，severity 降级 warning，verification 有审计） ---


async def test_uc58_fpr_downgrades_but_still_emits() -> None:
    storage = await make_storage()
    try:
        sample = MetricAnomaly(
            service="svc-a", metric="cpu_usage", value=0.95, method="static_threshold",
            severity="high", detected_at=TS,
        )
        gk = fingerprint.group_key("default", "application", "svc-a", [sample])
        storage.dynamic_config.seed_fpr("default", {gk: {"fpr": 0.9, "total": 50}})
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high")]
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.95, ts=TS)], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1  # 不永久静默
        rec = result.records[0]
        assert rec.severity == "warning"  # 降级
        assert rec.verification.false_positive_rate == 0.9
        assert rec.verification.final_severity == "warning"
    finally:
        await storage.close()


# --- UC-5.9：无信号提前终止（不开单，timeline collect_done 0） ---


async def test_uc59_no_signal() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high")]
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert result.records == []
        collect_done = next(t for t in result.timeline if t["step"] == "collect_done")
        assert collect_done["count"] == 0
    finally:
        await storage.close()


# --- UC-5.10：日志源超时降级（不崩溃，record 带 degraded 标记） ---


async def test_uc510_degraded_source_marked() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high")]
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.95, ts=TS)], degraded_sources=["MT-0001"], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1
        rec = result.records[0]
        degraded = next(e for e in rec.evidence if e["type"] == "degraded")
        assert degraded["target_ids"] == ["MT-0001"]
        assert degraded["round_id"] == rec.trace_id
        assert result.degraded_sources == ["MT-0001"]
    finally:
        await storage.close()


# --- trace_id 透传：采集器带业务 trace_id 时 → LogAnomaly.trace_ids → record.evidence ---


async def test_trace_ids_flow_into_problem_record_evidence() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="ERROR", plugin="signature_aggregate", params={"min_count": 2}, severity="high")]
        )
        signals = [
            log_signal(trace_id="tid-b"),
            log_signal(trace_id="tid-a"),
            log_signal(trace_id="tid-a"),
        ]
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=signals, domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1
        rec = result.records[0]
        # LogAnomaly 聚合去重 + 排序
        assert rec.log_anomalies[0].trace_ids == ["tid-a", "tid-b"]
        # emit 写 evidence 条目（去重、计数）
        trace_ev = next(e for e in rec.evidence if e["type"] == "log_trace_ids")
        assert trace_ev["trace_ids"] == ["tid-a", "tid-b"]
        assert trace_ev["count"] == 2
        # round 自己的 trace_id 独立于业务 trace_id（仍是 pipeline 单 trace_id）
        assert rec.trace_id
        # evidence 携带 round_id（= trace_id）以便跨轮追溯；未建 detection_round 时 target_ids 为空
        assert trace_ev["round_id"] == rec.trace_id
        assert trace_ev["target_ids"] == []
    finally:
        await storage.close()


async def test_no_trace_id_no_evidence_entry() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="ERROR", plugin="signature_aggregate", params={"min_count": 2}, severity="high")]
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[log_signal(), log_signal()], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.log_anomalies[0].trace_ids == []
        assert all(e["type"] != "log_trace_ids" for e in rec.evidence)
    finally:
        await storage.close()


async def test_evidence_carries_round_and_target_ids() -> None:
    """建 detection_round + target 后，log_trace_ids evidence 携带 round_id 与该轮 target_ids。"""
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="ERROR", plugin="signature_aggregate", params={"min_count": 2}, severity="high")]
        )
        round_id = "trace-round-1"
        await storage.rounds.create_round(
            "default", round_id, "application", started_at=TS, target_ids=["MT-0001", "MT-0006"]
        )
        for tid in ["MT-0001", "MT-0006"]:
            await storage.rounds.create_target("default", round_id, tid, started_at=TS)
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            trace_id=round_id,
            signals=[log_signal(trace_id="tid-x"), log_signal(trace_id="tid-x")], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1
        rec = result.records[0]
        trace_ev = next(e for e in rec.evidence if e["type"] == "log_trace_ids")
        assert trace_ev["round_id"] == round_id
        assert trace_ev["target_ids"] == ["MT-0001", "MT-0006"]
        assert trace_ev["trace_ids"] == ["tid-x"]
    finally:
        await storage.close()


# --- UC-5.11：单条 INFO 弱信号（不开单，不升级为事件） ---


async def test_uc511_single_info_weak_signal() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="INFO", plugin="signature_aggregate", params={"min_count": 5}, severity="warning")]
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[log_signal(level="INFO", message="hello", ts=TS)], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert result.records == []
        assert result.anomaly_count == 0
    finally:
        await storage.close()


# --- M9：按 signature / traceId 分组出单（跨服务合并） ---

LOG_DOMAIN = DomainConfig(
    detectors=[DetectorSpec(signal="ERROR", plugin="signature_aggregate", params={"min_count": 1}, severity="high")],
    verify=VerifySpec(persistence_rounds=1),
)


async def _run_logs(storage, signals, *, now=TS, domain_config=None):
    registry = PluginRegistry().load()
    ctx = await build_context(
        tenant_id="default", domain="application", registry=registry, storage=storage, now=now,
        signals=signals, domain_config=domain_config or LOG_DOMAIN,
    )
    return ctx, await run_domain(ctx)


async def test_m9_distinct_error_types_open_separate_records() -> None:
    """3 类不同 error、无共同 traceId → **3 条**记录（不是按 service 合成一条）。"""
    storage = await make_storage()
    try:
        _, result = await _run_logs(storage, [
            log_signal(signature="ErrA", trace_id="t1"),
            log_signal(signature="ErrB", trace_id="t2"),
            log_signal(signature="ErrC", trace_id="t3"),
        ])
        assert result.anomaly_count == 3
        assert len(result.records) == 3
        assert sorted(r.log_anomalies[0].signature for r in result.records) == ["ErrA", "ErrB", "ErrC"]
    finally:
        await storage.close()


async def test_m9_shared_trace_id_merges_into_one_record() -> None:
    """1 个 traceId 报了 3 类不同错误 → **1 条**记录（同一次请求失败），签名全在里面。"""
    storage = await make_storage()
    try:
        _, result = await _run_logs(storage, [
            log_signal(signature="ErrA", trace_id="t1"),
            log_signal(signature="ErrB", trace_id="t1"),
            log_signal(signature="ErrC", trace_id="t1"),
        ])
        assert result.anomaly_count == 3
        assert len(result.records) == 1
        rec = result.records[0]
        assert {a.signature for a in rec.log_anomalies} == {"ErrA", "ErrB", "ErrC"}
        # traceId 透传进 evidence
        trace_ev = next(e for e in rec.evidence if e["type"] == "log_trace_ids")
        assert trace_ev["trace_ids"] == ["t1"]
    finally:
        await storage.close()


async def test_m9_trace_spans_services_joins_service_names() -> None:
    """traceId 跨服务 → 1 条记录，service 是排序后的拼接串，group_key 用代表服务。"""
    storage = await make_storage()
    try:
        _, result = await _run_logs(storage, [
            log_signal(service="order-service", signature="ErrA", trace_id="t1"),
            log_signal(service="gateway-service", signature="ErrB", trace_id="t1"),
        ])
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.service == "gateway-service,order-service"
        # group_key 的 service 段取代表（排序首个），**不是**拼接串——否则会撑爆
        # group_key/open_group_key/唯一索引的 VARCHAR(255)
        assert rec.group_key_service == "gateway-service"
        tenant, domain, svc, _hash = rec.group_key.split(":")
        assert (tenant, domain, svc) == ("default", "application", "gateway-service")
    finally:
        await storage.close()


async def test_m9_group_key_stable_across_rounds() -> None:
    """同签名跨轮复发 → **追加**（occurrence_count++），不是每轮开新单。

    这条守的是 group_key 稳定性：代表服务或分组不稳定都会让去重失效，
    表现为「同一个问题每轮多一条记录」。
    """
    storage = await make_storage()
    try:
        for i in range(2):
            await _run_logs(
                storage,
                [log_signal(signature="ErrA", trace_id=f"t{i}"), log_signal(signature="ErrB", trace_id=f"u{i}")],
                now=TS + timedelta(seconds=60 * i),
            )
        rows = await storage.records.list("default")
        assert len(rows) == 2, f"应稳定为 2 条（两类 error），实际 {len(rows)}"
        assert all(r["occurrence_count"] == 2 for r in rows), "第二轮应是追加而非新开单"
    finally:
        await storage.close()


async def test_m9_seen_keys_covers_every_anomaly() -> None:
    """分组必须是划分：每个异常都恰好进一次 l3_verify。

    漏掉 → sweep 误判 miss → reconciler 自动关掉活着的单；重复 → consecutive_rounds
    双增 → 持续性闸门被绕过。两种都静默，所以直接断言 seen_keys 的覆盖。
    """
    storage = await make_storage()
    try:
        ctx, _ = await _run_logs(storage, [
            log_signal(signature="ErrA", trace_id="t1"),
            log_signal(service="svc-b", signature="ErrB", trace_id="t1"),
            log_signal(signature="ErrC", trace_id="t2"),
        ])
        expected = {a.anomaly_key() for a in ctx.anomalies}
        assert ctx.seen_keys == expected, "有异常没进 l3_verify（或重复进了）"
    finally:
        await storage.close()


async def test_m9_cross_service_metric_log_not_related() -> None:
    """metric 与 log **不同服务**时不得判为 related —— 否则会误升 critical。

    ``calibrate_severity`` 只看 ``related``，自己从不检查 service；同服务的保证原先靠
    「按 service 分组」隐式成立。M9 分组可跨服务，若 ``_within_window`` 不显式限定同服务，
    这个场景会误判：metric@svc-b 与远在窗口外的 log@svc-b 不算相关，却会跟同时刻的
    log@svc-a（不同服务）配上 → related=True → 误升 critical。
    """
    combo = DomainConfig(
        detectors=[
            DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high"),
            DetectorSpec(signal="ERROR", plugin="signature_aggregate", params={"min_count": 1}, severity="high"),
        ],
        correlation=CorrelationSpec(metric_log_window_sec=300),
        verify=VerifySpec(persistence_rounds=1),
    )
    storage = await make_storage()
    try:
        _, result = await _run_logs(storage, [
            # 同 traceId 把两个服务的日志连成一组；metric 属于 svc-b
            log_signal(service="svc-a", signature="ErrA", trace_id="t1", ts=TS),
            log_signal(service="svc-b", signature="ErrB", trace_id="t1", ts=TS + timedelta(minutes=10)),
            metric_signal(service="svc-b", value=0.95, ts=TS),
        ], domain_config=combo)
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.correlation.related is False, "metric@svc-b 与 log@svc-a 不同服务，不该判同源"
        assert rec.severity == "high", "related=False 时不应组合升 critical"
    finally:
        await storage.close()
