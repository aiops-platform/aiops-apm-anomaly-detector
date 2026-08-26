# M5 漏斗 L0–L3 + emit（确定性核心）— 历史规格归档

> 本文归档 `docs/apm-alert-implementation-plan-enhanced.md` 中 M5 小节（漏斗 L0–L3 + emit）**已实现**的部分。实现日志见 [`docs/logs/M5.md`](../logs/M5.md)，实现计划见 [`docs/plans/M5-implementation-plan.md`](../plans/M5-implementation-plan.md)。

## 目标

一个 `(tenant_id, domain)` 内的完整确定性漏斗可独立运行（暂不接 scheduler）：`collect（已预填 ctx.signals）→ L0 抑制 → L1 检测 → L2 关联 → L3 验证 → emit`。依赖：M1（契约 + fingerprint）、M2（store + 原子去重）、M4（detector/suppressor 插件 + registry + filter_signals）。关键修正 P0#1/#2/#3/#7/#8/#9 在此落地——**最高风险阶段**，用 §13 用例做 TDD 驱动。

## 完成标准

§13 用例 1/3/4/5/6/7/8/9/10/11 全部通过（InMemoryStore + mock collector 信号预填）。**用例 2（内存泄漏 + Full GC 组合升 critical）端到端延迟 M6**（Enhanced plan line 1518 明确）。

## 数据流

```
build_context() 载入 domain_config + maintenance_windows + blacklist + fpr + changes（storage.dynamic_config）
        │
        ▼
run_domain(ctx)
  1. timeline: collect_done(count)                     ← M5 消费 ctx.signals（scheduler 预填属 M6）
  2. L0 l0_suppress(ctx)         → ctx.suppressed / ctx.signals=kept
  3. L1 l1_detect(ctx)           → ctx.anomalies（filter_signals + registry.get("detector",...)）
  4. L2 l2_correlate(ctx)        → {service: (Correlation, change_related, recent_change)}
  5. 按 service：l3_verify(ctx, service, anoms) → Verification（state_store 持续性 + fpr 闸门 + 校准）
                  emit(ctx, ...) → ProblemRecord → storage.records.write_or_append（原子去重）
  6. state_store.sweep(seen_keys) → 未出现 key 记 miss_rounds
  7. timeline: record_created → DomainResult
```

## 交付（11 个 Use Case，对应 §13 用例 1/3/4/5/6/7/8/9/10/11）

| M5 | 名称 | 断言 |
|----|------|------|
| UC-5.1 | CPU 飙高两轮 | 第一轮不开单；第二轮 1 条 record；severity=high |
| UC-5.3 | 47 条 OOM 日志聚合 | 47 条 → 1 条 anomaly count=47；纯日志开单（log_only） |
| UC-5.4 | 指标+日志同源关联 | 只有 1 条 record；related=true；含 metric+log anomalies |
| UC-5.5 | 错误率突增 + 部署变更 | change_related=true；recent_change 含 change_id+summary |
| UC-5.6 | 瞬时抖动过滤 | 三轮均不开单；detection_state 反映 consecutive/miss rounds |
| UC-5.7 | 维护窗口抑制 | 不开单；suppressed 审计有记录；suppressed_count=1 |
| UC-5.8 | 误报率闸门 | 仍开单（不永久静默）；severity 降级 warning；有审计 |
| UC-5.9 | 无信号提前终止 | 不开单；timeline 记录 collect_done 0（零 LLM 调用） |
| UC-5.10 | 日志源超时降级 | 服务不崩溃；record 带 degraded 标记；degraded_sources 含 target_id |
| UC-5.11 | 单条 info 弱信号 | 不开单；1 条弱信号不升级为事件 |

## Pipeline 骨架（M5 落地 `pipeline/`）

- **context.py** — `DetectionContext`（`@dataclass(kw_only=True)`，P0#7）：`trace_id`/`tenant_id`/`domain`/`domain_config`/`registry`/`storage`/`now` + `targets`/`signals`/`changes`/`suppressed`/`anomalies` + `maintenance_windows`/`blacklist`/`fpr` + `degraded_sources`/`round_started_at`/`seen_keys` + 注入 `state_store`/`sequence_store`。实现用 M1 冻结 `DomainConfig` typed 字段（骨架用 raw dict）。`build_context` 从 `DomainConfigLoader` 载入 domain_config + 从 `storage.dynamic_config` 载入四类动态配置。`DomainResult(domain, records, suppressed_count, anomaly_count, degraded_sources, timeline)`。
- **l0_suppress.py** — 遍历 `suppressors`，`registry.get("suppressor", name)` → `batch_check(signals, ctx, params)`，产出 `[{signal, reason, suppressor}]` 并从 `ctx.signals` 移除。
- **l1_detect.py** — 遍历 `detectors`：`filter_signals(signals, dc.signal)` → `detector.detect(matched, params)`；单 detector 异常隔离（try/except 记 warning 不拖垮整轮）；`a.severity = dc.severity`（spec 权威覆盖，P1#12）；`MetricAnomaly.method = detector.name`。
- **l2_correlate.py** — 按 service 分组返回 `{service: (Correlation, change_related, recent_change)}`（骨架 2 元组，加 recent_change 满足 UC-5.5）。`_within_window`（指标+日志同源，默认 300s）、`_change_within_window`（service 匹配 + 时差 ≤ change_window_sec）、`template_summary`（模板兜底 `"{service} {metric} {value}"` / `"{service} {signature} x{count}"`，`；` 连接）。reason：`metric_log_within_window`/`metric_only`/`log_only`/`unrelated`。
- **l3_verify.py** — 持续性用 per-key `fingerprint.anomaly_key` 的 `consecutive_rounds`（P0#2 修正，废弃设计 §6.6 previous_keys 交集法）；**increment-first**：`new_consecutive = prev + 1` 再判 `>= persistence_rounds`（语义「连续 N 轮出现 → 第 N 轮开单」）。fpr 闸门：`fpr_ok = total < min_samples or fpr < false_positive_threshold`，命中只降级 `final_severity="warning"` **不丢弃**（P0#8）。`calibrate_severity` 取最高（组合升 critical 留 M6）。
- **emit.py** — `verification.passed` 才组装 `ProblemRecord`：`record_id` 从 sequence_store 取号、`symptom.summary` 用 `template_summary`、degraded 标记写 evidence、`write_or_append` 原子去重（M2 返回 None，直接返回 `[rec]`）。
- **runner.py** — `run_domain(ctx)`：timeline `collect_done → suppressed → detected → correlated → record_created`；按 service 循环 `l3_verify` + `emit`；最后 `state_store.sweep(tenant, domain, seen_keys)` 记 miss；返回 `DomainResult`。无信号提前终止天然成立（anomalies 空 → 无 L3/emit）。

## 新增存储（M5 落地 `storage/`，ABC + InMemory + MySQL 三件套）

| Store | 接口 | InMemory 真源 | MySQL 实现 |
|-------|------|--------------|-----------|
| `SequenceStore` | `next_id(domain) -> PR-YYYYMMDD-NNNN` | `_next[seq_date]` 自增，注入 `now` 可测跨日期 | `INSERT ... ON DUPLICATE KEY UPDATE next_seq = LAST_INSERT_ID(next_seq + 1)` + 同 handle `SELECT LAST_INSERT_ID()` |
| `DetectionStateStore` | `get/upsert/sweep(tenant, domain, key, seen_keys)` | `dict[(tenant,domain,key)]` | `state_value` JSON（datetime isoformat），sweep 用 `JSON_SET` UPDATE |
| `DynamicConfigStore` | `load_maintenance_windows/load_blacklist/load_fpr/load_changes(tenant)` | `seed_*` 预置行 | SELECT（blacklist 只取 `enabled=1`、backtick `signal`；fpr DECIMAL→float） |

每方法入口 `if not tenant_id: raise ValueError(...)`（多租户隔离硬约束）。

## 范围（不做，留后续里程碑）

- **用例 2 端到端**（内存泄漏 + Full GC 组合 → critical）— M6（Enhanced plan line 1518 明确）。
- **scheduler / poller / collect 编排 / degraded_sources 产生** — M6（M5 只消费 `ctx.signals`/`ctx.degraded_sources`，测试直接预填）。
- **`/v1/problems` / `/v1/detection-state` API 与 admin 写表 API** — M6（M5 只读表/纯函数）。
- **detector params 写入侧校验**（`ConfigValidationError`）— M6 config API。
- **fpr 回写、诊断闭环、问题单 resolve/自动关闭** — M6/v2 诊断。
- **LLM L2 摘要** — 设计「可选不阻塞」；M5 全走 `template_summary` 模板兜底（满足「零 LLM 调用」）。
- **MySQL store 真库实测** — 本机 MySQL 未运行（M3 遗留）；MySQL 实现照既有模式写出，InMemory 真源单测覆盖，待 DB 可用补跑。
