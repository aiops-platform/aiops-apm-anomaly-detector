"""测试床的**指标**监控端点 seed（幂等）：order-service 的 CPU 两条判据线。

给 minikube `order` 命名空间里的 order-service 建两条 `metric + prometheus` 端点，
由自带 scheduler 按 ``schedule.interval_sec`` 定时拉 Prometheus → L0–L3 → 开单，
最后出现在 Diagnosis Center 里等人点 Analyze。

用法：``python docker/seed_testbed_metrics.py``（读 .env / ``APM_`` 环境变量）或
``make seed-testbed-metrics``。

**与 ``seed_testbed_logs.py`` 的分工**：那个管日志（走 ES，按堆栈签名聚合）；
本文件管指标（走 Prometheus，判"持续"）。两边互不影响，可以各跑各的。

--- 为什么是两条线（而不是一条 CPU 曲线）-------------------------------------

判据分两层，各有各的问题，别混：

* **风险线 `cpu_usage`**：CPU 占 limit 的比例。回答"还有多少余量"。
  ⚠️ 它**不等于有人受影响** —— CPU 型业务本就该吃满配额。
* **症状线 `cpu_throttled_percent`**：被 CFS 限流的周期占比。回答"有没有人在等"。
  容器被掐住时请求**被推迟**，这是 CPU 饱和唯一直接可感的那一面。

**两条线用同一个窗口（3 分钟）**，因为 ``group_key`` 是"该轮异常集合"的哈希
（``models/fingerprint.py``）——集合一变就是**另一张单**。窗口错开的话，一次故障会
先后开出两张单，人要点两次 Analyze。同窗口则两条异常落在同一轮的同一个组里，
成一条记录、``metric_anomalies`` 里两条证据。

--- 判据为什么写在查询里（而不是靠 L3 数轮数）---------------------------------

采集器是 **instant query + 一行一信号**（``FieldMapper.map_metric``），每轮只能拿到
**一个标量**。所以让那个标量本身就回答"持续了吗"：

``min_over_time((比值)[3m:30s])`` —— 取窗口内每 30s 一次采样的**最小值**；
最小值都超过门限，才叫"整整 3 分钟都超"。这比"连续 N 轮超限"精确得多：
后者算的是**累计出现次数**（断轮不清零），而且窗口重叠时同一个样本会被重复计入。

三个配套写法各有原因：

* ``sum by (pod)``  —— 服务级求和的 3 副本里 1 个打满只显示 33%，永远不触发；
  按 pod 分组才能让"单副本跑飞"自己越线。
* ``>= bool``       —— 输出 0/1 而不是把不达标的行过滤掉 ⇒ **每轮每 pod 恒定一行**。
  于是 ``signals_count`` 成了采集健康度指标（空结果=采集坏了，看得出来），
  水位线也正常推进。**不要**去掉它改成过滤式。
* ``and on(pod) count_over_time(...) >= 4`` —— 样本不够就不判（Prometheus 重启、
  采集断档时避免拿一两个点下结论）。
* ``label_replace(...)`` —— ``sum()`` 会把标签（含 ``__name__``）全丢掉，映射取不到
  就**静默落成 ``"unknown"``**，而检测器按 ``signal: cpu_usage`` 匹配 ⇒ 永远不命中。

--- 选择器：``container!=""`` 不能写成 ``container!="POD"`` -------------------

cAdvisor 的 **pod 级聚合序列**的 ``container`` 标签是**空串**（不是 ``"POD"``），
所以 ``!="POD"`` 会把「pod 级 cgroup + 应用容器」都留下、**分子分母各算两遍**：
实测一个 ``limit=1`` 核的容器算出来是 **2 核**。比值因两边同倍放大而大致可用，
但任何"限值本身"的数就错了。（本仓 ``agentflow/datasource/app_indicators.py`` 早就
踩过并写了注释 + 回归测试，这边是新补上的。）

--- 量纲 --------------------------------------------------------------------

``>= bool`` 让输出恒为 0/1，**量纲问题自动消失**；域检测器的 ``threshold: 0.9``
只表示"等于 1 才异常"。所以这里不需要关心"APM 域约定是 0–1 还是 0–100"。

关联：``multi-agent-workflow/docs/todos/METRIC_PIPELINE_BEST_PRACTICE_zh-CN.md``（§4.2/§4.2.1）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from aiops_apm.models.config import DetectorSpec, DomainConfig
from aiops_apm.settings import Settings
from aiops_apm.storage import build_storage

TENANT = "default"
# 挂 application 域：它是 domains.yaml 里 seed 的域，活库里已有 enabled 的 domain_config
# （含 cpu_usage 那条检测器）。挂一个没有配置的域会导致漏斗无检测器可用 → 什么都不产出。
DOMAIN = "application"
INTERVAL_SEC = 60
SERVICE = "order-service"

# 判据参数（**运维参数**，改这里不用动提示词、也不用重跑模型）
WINDOW = "3m"          # "持续"的时长：整段窗口都超门限才算数
STEP = "30s"           # 子查询采样步长（5s 抓取下的粗粒度采样，抗抖动）
MIN_SAMPLES = 4        # 窗口内至少要有这么多个采样点，否则不判
CPU_THRESHOLD = 0.8    # 风险线：占 limit 的比例
# 症状线：被限流的周期占比（百分数）。
# 2026-09-29 曾试过抬到 50（想让两条线同时越线、避免"集合变大 ⇒ 另一张单"），
# **实测无效**：限流比 40 秒就到 99%，与门限取 5 还是 50 无关，真正决定越线时刻的是
# CPU 占 limit 那个**一分钟平均**的斜坡 ⇒ 已改回 5。
# 两张单的问题改在**去重侧**解决（records.write_or_append 的超集并入），不在这里凑。
THROTTLE_THRESHOLD = 5

_DOMAINS_YAML = Path(__file__).resolve().parent.parent / "src" / "aiops_apm" / "config" / "domains.yaml"


def _ratios(service: str) -> dict[str, str]:
    """两条**比值**表达式（还没套 min_over_time / bool）。"""
    sel = f'pod=~"{service}.*",container!="",container!="POD"'
    return {
        "cpu_usage": (
            f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{sel}}}[1m]))"
            f" / sum by (pod) (container_spec_cpu_quota{{{sel}}}"
            f" / container_spec_cpu_period{{{sel}}})"
        ),
        "cpu_throttled_percent": (
            f"100 * sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[1m]))"
            f" / sum by (pod) (rate(container_cpu_cfs_periods_total{{{sel}}}[1m]))"
        ),
    }


def _sustained(ratio: str, *, metric: str, service: str, threshold: float) -> str:
    """把「持续」编码进查询，返回 0/1（详见模块 docstring）。"""
    sub = f"({ratio})[{WINDOW}:{STEP}]"
    return (
        "(label_replace(label_replace("
        f"(min_over_time({sub}) >= bool {threshold})"
        f" and on(pod) (count_over_time({sub}) >= {MIN_SAMPLES}),"
        f'"metric", "{metric}", "pod", ".*"), '
        f'"service", "{service}", "pod", ".*"))'
    )


def _target(service: str, prom_url: str, label: str, metric: str, query: str) -> dict:
    return {
        "service": service,
        "signal_type": "metric",
        "source_type": "prometheus",
        "domain": DOMAIN,
        "schedule": {"interval_sec": INTERVAL_SEC},
        "source_config": {
            # 幂等判据的一部分（见 seed()）+ 人肉可读的标识。
            # 采集器**不认**这个键（它只读 url/method/headers/params/rows_path/
            # field_mapping/window_sec/time_params/timezone），多一个键会被忽略。
            "label": label,
            "url": prom_url,
            "method": "GET",
            "rows_path": "data.result",
            "params": {"query": query},
            "field_mapping": {
                "service": "metric.service",
                "metric": "metric.metric",
                "value": "value[1]",
                "timestamp": "value[0]",
                # ⚠️ 必须映射：Prometheus 把标签放在 **`metric` 对象**里，而
                # map_metric 还认得这个键（不认识时只能读行里顶层的 labels，
                # 于是 labels 恒为空 → 所有 pod 共享一个 anomaly_key）。
                "labels": "metric",
            },
        },
    }


def _lines(service: str, prom_url: str) -> list[dict]:
    ratios = _ratios(service)
    return [
        _target(service, prom_url, "cpu_risk", "cpu_usage",
                _sustained(ratios["cpu_usage"], metric="cpu_usage",
                           service=service, threshold=CPU_THRESHOLD)),
        _target(service, prom_url, "cpu_throttle", "cpu_throttled_percent",
                _sustained(ratios["cpu_throttled_percent"], metric="cpu_throttled_percent",
                           service=service, threshold=THROTTLE_THRESHOLD)),
    ]


def _signal_name(signal: str | dict) -> str:
    """``DetectorSpec.signal`` 两种形态（信号名 / 结构化 matcher）都归一成信号名。"""
    return signal if isinstance(signal, str) else str(signal.get("metric", ""))


def _domain_config_from_yaml() -> DomainConfig:
    """库里还没有这个域的行时，照 ``domains.yaml`` 的 seed 起一份（别凭空造）。"""
    raw = yaml.safe_load(_DOMAINS_YAML.read_text(encoding="utf-8"))
    item = next(d for d in raw["domains"] if d["id"] == DOMAIN)
    return DomainConfig.model_validate(item)


async def _ensure_detector(storage) -> None:
    """保证 ``application`` 域里有一条 ``cpu_throttled_percent`` 检测器。

    风险线复用已有的 ``cpu_usage`` 那条（不动）；症状线**必须**新增一条，
    而且必须用**不同的指标名** —— 否则两条线的 ``anomaly_key``
    （``metric|tenant|service|指标名|labels``）会撞在一起，记录里分不出
    "是 CPU 满还是被限流"，两条线还会共享同一个持续性计数器。
    """
    rows = await storage.domain_configs.load(TENANT)
    row = next((r for r in rows if r["domain"] == DOMAIN), None)
    cfg = DomainConfig.model_validate(row["config"]) if row else _domain_config_from_yaml()

    spec = DetectorSpec(
        signal="cpu_throttled_percent",
        plugin="static_threshold",
        # 信号是 0/1 ⇒ 0.9 等价于"等于 1 才异常"（真门限在查询里，见模块 docstring）
        params={"threshold": 0.9},
        severity="high",
    )
    kept = [d for d in cfg.detectors if _signal_name(d.signal) != "cpu_throttled_percent"]
    if len(kept) == len(cfg.detectors):
        print("[seed] detector 新增 cpu_throttled_percent → static_threshold(0.9, high)")
    else:
        print("[seed] detector 刷新 cpu_throttled_percent → static_threshold(0.9, high)")
    cfg.detectors = [*kept, spec]
    version = await storage.domain_configs.upsert(TENANT, DOMAIN, cfg)
    print(f"[seed] domain_config {DOMAIN} 已写入（version={version}，"
          f"detectors={[_signal_name(d.signal) for d in cfg.detectors]}）")


async def seed() -> None:
    settings = Settings()
    prom_url = settings.testbed_prom_url
    storage = await build_storage(settings)
    try:
        # 幂等判据 = (service, signal_type, source_config.label)。
        # ⚠️ 不能只用 (service, signal_type)：同一个服务下有**两条** metric 端点
        # （风险线 + 症状线），只用前两项会把它们当成同一条、互相覆盖。
        existing = {
            (t["service"], t["signal_type"], (t.get("source_config") or {}).get("label")): t
            for t in await storage.monitor_targets.list(TENANT)
        }
        for spec in _lines(SERVICE, prom_url):
            key = (spec["service"], spec["signal_type"], spec["source_config"]["label"])
            cur = existing.get(key)
            if cur is None:
                row = await storage.monitor_targets.create(TENANT, spec)
                print(f"[seed] created {row['target_id']}  metric@{SERVICE}"
                      f"  label={spec['source_config']['label']}  every {INTERVAL_SEC}s")
            else:
                # 已存在则**刷新**采集配置：本文件是这两条端点的唯一真源，
                # 只 skip 会让文件与活库分叉（改一次判据就得手工同步一次）。
                await storage.monitor_targets.update(
                    TENANT,
                    cur["target_id"],
                    {"source_config": spec["source_config"], "schedule": spec["schedule"]},
                )
                print(f"[seed] updated {cur['target_id']}  metric@{SERVICE}"
                      f"  label={spec['source_config']['label']}  every {INTERVAL_SEC}s")

        await _ensure_detector(storage)
    finally:
        await storage.close()


if __name__ == "__main__":
    asyncio.run(seed())
