"""测试床三服务的日志监控定时任务（monitor_target）seed。

给 minikube `order` 命名空间里的三个服务各建一个日志监控端点，由 APM 自带的
scheduler 按 ``schedule.interval_sec`` 定时触发采集 → L0–L3 漏斗 → 开单。

幂等：按 ``(service, signal_type)`` 判重——已存在则**刷新** ``source_config``/``schedule``，不存在才新建。
用法：``python docker/seed_testbed_logs.py``（读 .env / APM_ 环境变量）或 ``make seed-testbed``。

**与 V9 迁移的分工**：这三个端点已随 ``V9__seed_testbed_log_targets.sql`` 在 ``make migrate``
时种入，所以**全新库不需要跑本脚本**。迁移是一次性、不可变的历史——要改这三个端点的配置，
改本文件后重跑（会刷新活库），**不要改 V9**。
（两处内容目前一致；本文件权威，V9 是它的首次初始化快照。）

**V11 不接管这三个端点**：V11（活库快照）刻意不种 ``monitor_target``——V9 < V11，重复种也
永远输给 V9 的 ``ON CONFLICT DO NOTHING``，是死代码；而且 ``source_config`` 里内嵌 ES 地址，
V9 刻意走 ``aiops.testbed_es_url`` GUC 注入以保证环境可移植。所以本文件仍是这三个端点配置的
唯一可改入口。

--- 采集链路的事实（写配置前实测过，别凭猜改）---------------------------------

日志链路：三个服务 → filebeat（DaemonSet）→ Elasticsearch，索引 ``app-logs``。
ES 在集群里是 ``svc/elasticsearch:9200``，本机经 port-forward 映射到 ``19200``。

一条 ``_search`` 返回的文档形状（截取自真实响应）::

    hits.hits[]._source = {
        "@timestamp": "2026-09-16T09:51:36.069Z",     # UTC 带 Z
        "app": {"service": "order-service", "level": "WARN",
                "message": "Resolved [org.springframework...]",
                "traceId": "4069fb8e-...", "logger_name": "...", "thread_name": "..."}
    }

字段映射的契约：采集器**不剥** ``_source`` 外壳，``field_mapping`` 的路径直接带
``_source.`` 前缀去取（M3 起就是这约定，见 ``tests/test_collectors.py`` 的 ``_log_target``）。
``rows_path`` 填 ``hits.hits`` 定位到命中数组，每条仍是 ``{_index,_id,_score,_source}``
的包装器，由带前缀的路径穿透到内层。

--- 实测数据特征（169 条样本）-------------------------------------------------

- ``@timestamp`` 在 ``_source`` **顶层**，每条都有。
- ``app.*``（service/level/message/traceId）只有 **109/169 条**有——另外约 35% 是
  非 JSON 行（Spring 启动横幅等），压根没有 ``app`` 对象。映射取不到时
  ``map_log`` 的 ``service`` 回落为 ``"unknown"``；**因此按服务过滤必须在 ES 查询里做**
  （见 ``service_field``），不能指望采集后再筛。
- ``@timestamp`` 是 UTC 带 ``Z``，故不设 ``timezone``。

--- 查询下发方式 -------------------------------------------------------------

``time_field`` / ``service_field`` / ``level_field`` 三个键是**给采集器看的开关**：设了它们，
``collectors/http_logs.py`` 的 ``_build_body`` 才会构造 ES 的 POST body——

- ``service_field``：按 ``target["service"]`` 做 term 过滤（ES 侧要 ``.keyword`` 后缀）；
- ``time_field``：把水位线增量窗口做成 body 里的 ``range`` filter。**ES 的 URI 查询不支持
  日期 range**，所以时间窗只能走 body；不下发的话每轮都重采最新一页、水位线推不动。
- ``level_field`` + ``levels``：按级别做 ``terms`` 过滤（同样要 ``.keyword`` 后缀、同样精确大小写）。

非 ES 源不设这些键，采集器返回 ``None`` body，行为与改动前一致。

另注：``app-logs`` 里 ERROR 是少数派但**确实存在**（2026-09-23 实测：全库 31 条，均带
``stack_trace``；INFO 是压倒性多数，单次洪峰 4~7 万条）—— 这正是上面 ``level_field``
存在的原因：不在 ES 侧挡掉 INFO，``signature_aggregate`` 拿不到 ERROR 的输入。
"""

from __future__ import annotations

import asyncio

from aiops_apm.settings import Settings
from aiops_apm.storage import build_storage

TENANT = "default"
# 挂 application 域：它是 domains.yaml 里 seed 的域，活库里已有 enabled 的 domain_config。
# 挂一个没有配置的域会导致漏斗无检测器可用 → 什么都不产出。
DOMAIN = "application"
INTERVAL_SEC = 60

# 三个被监控的服务（实测自 app-logs 的 app.service.keyword 聚合）
# ES 地址取 settings.testbed_es_url（APM_TESTBED_ES_URL），与 V9 同源
SERVICES = ["order-service", "warranty-service", "gateway-service"]


def _target(service: str, es_url: str) -> dict:
    return {
        "service": service,
        "signal_type": "log",
        "source_type": "elk",
        "domain": DOMAIN,
        "schedule": {"interval_sec": INTERVAL_SEC},
        "source_config": {
            "url": es_url,
            "method": "POST",
            "rows_path": "hits.hits",
            # 时间/服务过滤字段：ES 侧要带 .keyword 后缀才能做精确 term 聚合，
            # 而 _source 里的取值路径不带（见模块 docstring 的文档形状）
            "time_field": "@timestamp",
            "service_field": "app.service.keyword",
            # 只取 ERROR（2026-09-23 加）：这三条 target 的 INFO 洪峰（实测单次 4~7 万条挤在
            # 0.7 秒内）会把采集器每轮 500 条的 size 上限顶满，水位线一轮只推进几毫秒 →
            # 积压永久累积、真正的 ERROR 永远轮不到（当时滞后 111,418 条 ≈ 4 小时）。
            # 全库 ERROR 仅 31 条，加了它采集量降到可忽略，且同签名的 ERROR 能落在同一轮里
            # 过 signature_aggregate 的 min_count=5 门槛。
            # 取值须与源端**大小写完全一致**：.keyword 是精确 term，实测 ["error"] 匹配 0 条。
            "level_field": "app.level.keyword",
            "levels": ["ERROR"],
            # @timestamp 本身就是 UTC 带 Z，故不设 timezone（设了反而会二次偏移）
            # 路径带 _source. 前缀：采集器不剥壳（见模块 docstring 的契约说明）
            "field_mapping": {
                "service": "_source.app.service",
                "level": "_source.app.level",
                "message": "_source.app.message",
                "timestamp": "_source.@timestamp",
                "trace_id": "_source.app.traceId",
                # stack_trace 必须映射：signature() 有堆栈时取「异常首行|顶部N帧」，
                # 缺了它回退到 message[:120] —— 而 Spring 的 "Servlet.service() for servlet
                # [dispatcherServlet] ... threw exception [Request processing failed: ..."
                # 前缀对**所有**异常都一样，真正的异常类型在 120 字符之外被截掉，
                # 于是不同类型的 error 全塌成同一个签名、归成同一条记录（M9 要的正好相反）。
                "stack_trace": "_source.app.stack_trace",
            },
        },
    }


async def seed() -> None:
    settings = Settings()
    # ES 地址与 V9 迁移同源（都取 settings.testbed_es_url）——否则设了
    # APM_TESTBED_ES_URL 时，V9 用新值而本脚本把它覆盖回硬编码的 localhost。
    es_url = settings.testbed_es_url
    storage = await build_storage(settings)
    try:
        existing = {
            (t["service"], t["signal_type"]): t
            for t in await storage.monitor_targets.list(TENANT)
        }
        for service in SERVICES:
            spec = _target(service, es_url)
            cur = existing.get((service, "log"))
            if cur is None:
                row = await storage.monitor_targets.create(TENANT, spec)
                print(f"[seed] created {row['target_id']}  log@{service}  every {INTERVAL_SEC}s")
            else:
                # 已存在则刷新采集配置：本文件是这三个端点的唯一真源，
                # 只 skip 会让文件与活库分叉，改一次配置就得手工同步一次。
                await storage.monitor_targets.update(
                    TENANT,
                    cur["target_id"],
                    {"source_config": spec["source_config"], "schedule": spec["schedule"]},
                )
                print(f"[seed] updated {cur['target_id']}  log@{service}  every {INTERVAL_SEC}s")
    finally:
        await storage.close()


if __name__ == "__main__":
    asyncio.run(seed())
