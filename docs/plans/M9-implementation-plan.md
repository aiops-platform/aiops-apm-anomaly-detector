# M9 实现计划：日志异常按 signature / traceId 分组出单（跨服务合并）

## 背景与目标

`run_domain` 原先把一轮内异常**按 service 分组**，每个 service 一条 `problem_record`。两个方向都不对：

- 一个服务报 3 类不同错误 → 只有 **1 条**记录。根因、处理方式、历史误报率都不同，混在一起无法分别关单，`fpr_table` 统计也串味。
- 同一次请求失败在多处打的日志（共享业务 `traceId`，可能跨 gateway/order/warranty）本质是**一个**事故，却按服务裂成多条。

**目标**：以「日志签名 + 业务链路 id」重新分组出单。

## 设计决策

| # | 决策 |
|---|---|
| 1 | 分组算法 = **连通分量**：「同 `signature`」**或**「同 `trace_id`」任一成立即归一组 |
| 2 | **可跨服务**合并 |
| 3 | `service` 列 = 组内全部服务名逗号拼接（排序后） |
| 4 | metric 异常挂到该 service 的日志组（保住同源升 critical） |
| 5 | **即时开单**（`persistence_rounds=1`） |
| 6 | 检测器匹配 **ERROR** 级 |

## 分组算法

新模块 `pipeline/grouping.py`，纯函数：

```
1. 建并查集；以日志异常为节点
2. 同 signature 的日志异常 → union
3. trace_ids 相交的日志异常 → union
4. metric 异常：union 到「该 metric.service 的日志异常」所在分量
   —— 仅当该服务的日志异常同属一个分量时；否则 metric 自成一組
   （避免把该服务多个不同类型的日志组强行合并，那正是本阶段要避免的）
5. 分量 → 组；service 列表排序拼接；代表服务 = 排序首个
```

**契约：`group_anomalies` 是划分**（两两不交、并集 == 输入）。`run_domain` 依赖它：每个异常必须恰好进一次 `l3_verify`。

## 必须一并修的既有缺陷

**`signature_aggregate` 在校验前就跨服务塌陷**（`detectors/signature_aggregate.py:28,39`）：只用 `signature` 分组、`service` 取 `logs[0].service`（取决于信号顺序）。两个服务打出同一签名会塌成一条 `LogAnomaly`，而 `anomaly_key` 把这个任意的 service 焙进去重身份 → 持续性与 reconcile 在服务间漂移。分组依赖 `LogAnomaly.service` 正确，必须先修。

## 改动文件

- **新增**：`pipeline/grouping.py`、`migrations/V10__widen_problem_record_service.sql`
- **改**：`pipeline/runner.py`（按组循环 + `records_by_service` credit-all）、`pipeline/l2_correlate.py`（按组 + `_within_window` 限定同 service）、`pipeline/l3_verify.py`（组代表）、`pipeline/emit.py`（`group_key_service`）、`models/record.py`（新增可选字段）、`detectors/signature_aggregate.py`（分组键加 service）、`storage/records.py`（service 成员匹配）、`router/problems.py`（`_primary_service`）
- **测试**：新增 `tests/test_grouping.py`；`test_pipeline.py` 加 6 条 M9 端到端；`test_records.py` / `test_pg_integration.py` / `test_l2.py` / `test_migrations.py` 同步

## 验收标准

1. `make lint` 干净（仅剩 2 条本次之前就存在的 ruff 错误）。
2. `make test` 全绿，集成道自动 skip。
3. `make test-pg` 全绿。
4. 四条分组场景命中：多 trace 同签名→1 条；同 trace 多签名→1 条；无关联→多条；trace 跨服务→1 条且 service 是拼接串。
5. 同签名跨轮 → `occurrence_count` 递增而非新开单。
6. `ctx.seen_keys` 恰等于本轮全部 `anomaly_key`。
7. `test_uc62_combo_critical.py` 不回归；跨服务 metric/log 判 `related=False`。
8. V10 后真库能写入 >64 字符的拼接 service。

## 风险与对策

见 `docs/logs/M9.md` 的「五个静默破坏点与对策」——`seen_keys` 覆盖、`group_key` 稳定性、`records_by_service` 算术、`related` 语义、`signature_aggregate` 塌陷。这五条现有测试都抓不到，实现时逐条设防。
