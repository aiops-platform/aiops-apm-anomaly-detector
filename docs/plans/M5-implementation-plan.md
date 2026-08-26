# M5 漏斗 L0–L3 + emit（确定性核心）— 实现计划

> 状态：**已实现**（2026-08-26，`make lint test` 全绿 225 用例）。实现日志见 `docs/logs/M5.md`，历史规格归档见 `docs/archive/M5-funnel.md`。

## Context（为什么做）

- **前置**：M0–M4 已完成（`make lint test dev` 全绿，182 用例，M4 已提交 `027cc80`）。已就位：契约模型（M1，含 `DomainConfig`/`DetectorSpec`/`Correlation`/`Verification`/`ProblemRecord`/`fingerprint`）、`RecordStore.write_or_append` 原子去重（M2）、`DomainConfigLoader`（M2）、`PluginRegistry.get` + `filter_signals`（M4）、内置 3 detector + 2 suppressor（M4）。**V1 DDL 已有 `record_seq`/`detection_state`/`fpr_table`/`maintenance_window`/`suppress_blacklist`/`change_record` 六张表，但均无 store 类**。
- **M5 是什么**：漏斗主体——一个 `(tenant_id, domain)` 内可独立运行的完整确定性 pipeline：`collect(已在 ctx.signals) → L0 抑制 → L1 检测 → L2 关联 → L3 验证 → emit`。暂不接 scheduler、不加 API。
- **完成标准**（Enhanced plan M5 原文）：§13 用例 1/3/4/5/6/7/8/9/10/11 全部通过（InMemoryStore + mock collector）。**用例 2（内存泄漏组合升 critical）端到端延迟到 M6**（Enhanced plan line 1518 明确）。
- **蓝图**：`docs/apm-alert-implementation-plan-enhanced.md` M5 小节骨架（`pipeline/context.py`/`l0_suppress`/`l1_detect`/`l2_correlate`/`l3_verify`/`emit`/`run_domain`）。实现以 Enhanced plan 骨架为准；设计文档 §6 与骨架有差异处已在下文标注。

## 改动点：位置与用途

```
src/aiops_apm/
├── pipeline/
│   ├── context.py             # 新增：DetectionContext + DomainResult + new_trace_id + build_context
│   ├── l0_suppress.py         # 新增：L0 批量抑制（消费 ctx.registry + ctx.maintenance_windows/blacklist）
│   ├── l1_detect.py           # 新增：L1 按 detector 规则分发（消费 filter_signals + registry）
│   ├── l2_correlate.py        # 新增：L2 按 service 关联（同源 + 变更）+ template_summary
│   ├── l3_verify.py           # 新增：L3 持续性 + 误报率闸门 + 严重度校准（消费 detection_state/fpr）
│   ├── emit.py                # 新增：组装 ProblemRecord + write_or_append 原子去重落库
│   └── runner.py              # 新增：run_domain 串行编排 + timeline + 按 service 的 L3/emit + miss sweep
├── storage/
│   ├── sequence.py            # 新增：SequenceStore（record_seq 取号 PR-YYYYMMDD-NNNN）
│   ├── detection_state.py     # 新增：DetectionStateStore（L3 持续性 consecutive/miss 计数）
│   ├── dynamic_config.py      # 新增：DynamicConfigStore（maintenance_window/suppress_blacklist/fpr_table/change_record 读取进 ctx）
│   └── __init__.py            # 改：Storage 聚合 + build_storage 挂 sequence/detection_state/dynamic_config
tests/
├── test_sequence.py           # 新增：next_id 格式/递增/日期切分
├── test_detection_state.py    # 新增：get/upsert/sweep/miss 语义
├── test_dynamic_config.py     # 新增：四类读取（含 tenant 过滤/只取 enabled）
├── test_l2.py                 # 新增：_within_window/_change_within_window/template_summary 纯函数
├── test_l3.py                 # 新增：calibrate_severity/fpr 闸门（降级不丢弃）/persistence 门
└── test_pipeline.py           # 新增：§13/UC-5.x 端到端（run_domain + build_context），11 用例
docs/plans/M5-implementation-plan.md   # 本文档（落库后状态 进行中 → 已实现）
docs/logs/M5.md                          # 实现日志
docs/archive/M5-funnel.md                # 归档已实现章节
README.md / CLAUDE.md                     # 进度同步
```

**数据流**（M5 不实现 collect，`ctx.signals` 由调用方/测试预填；M6 scheduler 才跑 collect）：

```
build_context() 载入 domain_config + maintenance_windows + blacklist + fpr + changes
        │
        ▼
run_domain(ctx)
  1. timeline: collect_done(count)
  2. L0 l0_suppress(ctx)         → ctx.suppressed / ctx.signals=kept
  3. L1 l1_detect(ctx)           → ctx.anomalies（filter_signals + registry.get("detector",...)）
  4. L2 l2_correlate(ctx)        → {service: (Correlation, change_related, recent_change)}
  5. 按 service：l3_verify(ctx, service, anoms) → Verification（state_store 持续性 + fpr 闸门 + 校准）
                  emit(ctx, ...) → ProblemRecord → storage.records.write_or_append（原子去重）
  6. state_store.sweep(seen_keys) → 未出现 key 记 miss_rounds
  7. timeline: record_created → DomainResult
```

## 范围

### 交付（11 个 Use Case，对应 §13 用例）

| M5 | 名称 | 断言 |
|----|------|------|
| UC-5.1 | CPU 飙高两轮 | 第一轮不开单；第二轮 1 条 record；severity=high |
| UC-5.3 | 47 条 OOM 日志聚合 | 47 条 → 1 条 anomaly count=47；纯日志开单 |
| UC-5.4 | 指标+日志同源关联 | 只有 1 条 record；related=true；含 metric+log anomalies |
| UC-5.5 | 错误率突增 + 部署变更 | change_related=true；recent_change 含 change_id+summary |
| UC-5.6 | 瞬时抖动过滤 | 三轮均不开单；detection_state 反映 consecutive/miss rounds |
| UC-5.7 | 维护窗口抑制 | 不开单；suppressed 审计有记录；suppressed_count=1 |
| UC-5.8 | 误报率闸门 | 仍开单（不永久静默）；severity 降级 warning；有审计 |
| UC-5.9 | 无信号提前终止 | 不开单；timeline 记录 collect_done 0 |
| UC-5.10 | 日志源超时降级 | 服务不崩溃；record 带 degraded 标记；degraded_sources 含 target_id |
| UC-5.11 | 单条 info 弱信号 | 不开单；1 条弱信号不升级为事件 |

> **用例 2（内存泄漏组合 → critical）端到端延迟 M6**；M5 `calibrate_severity` 先取最高 severity（组合升级 M6 补）。

### 不做（明确排除）
- **scheduler / poller / collect 编排 / degraded_sources 产生** — M6（M5 只消费 `ctx.signals`/`ctx.degraded_sources`，测试直接预填）。
- **`/v1/problems` / `/v1/detection-state` API 与 admin 写表 API** — M6（M5 只读表/纯函数）。
- **detector params 写入侧校验**（`ConfigValidationError`）— M6 config API。
- **fpr 回写、诊断闭环、问题单 resolve/自动关闭** — M6/v2 诊断。
- **LLM L2 摘要** — 设计「可选不阻塞」；M5 全走 `template_summary` 模板兜底（满足「零 LLM 调用」）。
- **MySQL store 真库实测** — 本机 MySQL 未运行（M3 遗留）；新 store 的 MySQL 实现照既有模式写出，由 InMemory 真源单测覆盖，真库验证待 DB 可用补跑。

## 关键实现细节

### 0. 先提交 M4 后续的文档状态（无代码）
- M5 落库本文档到 `docs/plans/M5-implementation-plan.md`，`CLAUDE.md`「当前里程碑」标注「M5 进行中」。

### 1. `pipeline/context.py` — DetectionContext + DomainResult + build_context
```python
def new_trace_id() -> str:
    return f"trace-{uuid.uuid4().hex[:24]}"

@dataclass(kw_only=True)          # 骨架 P0#7 修正
class DetectionContext:
    trace_id: str = field(default_factory=new_trace_id)
    tenant_id: str = "default"
    domain: str
    domain_config: DomainConfig    # 用 M1 冻结模型（骨架用 raw dict；改为 typed 字段访问，语义等价）
    registry: PluginRegistry
    storage: RecordStore
    state_store: DetectionStateStore
    sequence_store: SequenceStore
    now: datetime
    # 本轮数据
    targets: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    changes: list = field(default_factory=list)      # ChangeSignal 列表（L2 变更关联）
    suppressed: list = field(default_factory=list)   # [{"signal","reason","suppressor"}]
    anomalies: list = field(default_factory=list)
    # 动态配置（build_context 从表载入）
    maintenance_windows: list = field(default_factory=list)
    blacklist: list = field(default_factory=list)
    fpr: dict = field(default_factory=dict)          # {group_key: {"fpr": float, "total": int}}
    # 轮次状态
    degraded_sources: list = field(default_factory=list)
    round_started_at: datetime | None = None
    seen_keys: set = field(default_factory=set)      # L3 记本轮到到的 anomaly_key，run_domain sweep 用

@dataclass
class DomainResult:
    domain: str
    records: list
    suppressed_count: int
    anomaly_count: int
    degraded_sources: list
    timeline: list

async def build_context(*, tenant_id, domain, registry, storage: Storage, now, trace_id=None,
                        signals=None, changes=None, degraded_sources=None, domain_config=None) -> DetectionContext:
    """载入 domain_config（DomainConfigLoader）+ 四类动态配置（storage.dynamic_config）+ 注入 state/sequence store。"""
```
- `domain_config` 优先显式传；缺省用 `DomainConfigLoader(storage.domain_configs).load(tenant_id)` 找该 domain 行 → `DomainConfig.model_validate(row["config"])`。
- 动态配置从 `storage.dynamic_config` 读：`load_maintenance_windows`/`load_blacklist`/`load_fpr`/`load_changes`（changes 转 `ChangeSignal`，`changed_at→timestamp`）。
- 注入 `state_store=storage.detection_state`、`sequence_store=storage.sequence`、`storage=storage.records`。

### 2. `storage/sequence.py` — SequenceStore（ABC + InMemory + MySQL）
- `async def next_id(domain: str) -> str` → `PR-YYYYMMDD-NNNN`（NNNN=该日自增，`%04d`）。domain 参数保留（骨架签名），InMemory 忽略。
- InMemory：`_next[seq_date]` 自增。MySQL：`INSERT INTO record_seq (seq_date, next_seq) VALUES (%s,1) ON DUPLICATE KEY UPDATE next_seq=last_insert_id(next_seq+1)` 后 `SELECT next_seq`（原子取号，best-effort；M5 以 InMemory 真源为准）。

### 3. `storage/detection_state.py` — DetectionStateStore（ABC + InMemory + MySQL）
- `get(tenant, domain, key) -> dict | None`：`{"consecutive_rounds", "miss_rounds", "first_seen", "last_seen"}`（无则 None）。
- `upsert(tenant, domain, key, *, consecutive_rounds, miss_rounds, first_seen, last_seen) -> None`：首见 first_seen=now，last_seen=now，consecutive+1，miss=0。
- `sweep(tenant, domain, seen_keys: set) -> None`：本域 store 里**本轮到到**（∉ seen_keys）的 key → `miss_rounds+1, consecutive_rounds=0`（UC-5.6 关键）。
- MySQL：state_value 存 JSON `{"consecutive_rounds":..., "miss_rounds":..., "first_seen": iso, "last_seen": iso}`；sweep 用 `SELECT state_key` + 循环 `JSON_SET` UPDATE。

### 4. `storage/dynamic_config.py` — DynamicConfigStore（ABC + InMemory + MySQL，设计 §9）
- `load_maintenance_windows(tenant) -> list[dict]`：`maintenance_window` 行 `{service, start_at, end_at, reason}`（datetime 保持）。
- `load_blacklist(tenant) -> list[dict]`：`suppress_blacklist` 行 **enabled=1** → `{domain, service, signal, reason}`。
- `load_fpr(tenant) -> dict[group_key -> {"fpr": float, "total": int}]`：`fpr_table` 行（fpr DECIMAL→float）。
- `load_changes(tenant) -> list[dict]`：`change_record` 行 `{change_id, service, type, summary, changed_at}`。

### 5. `pipeline/l0_suppress.py`
```python
async def l0_suppress(ctx) -> None:
    kept, suppressed = [], []
    for sc in ctx.domain_config.suppressors:          # SuppressorSpec
        sup = ctx.registry.get("suppressor", sc.name)
        results = await sup.batch_check(ctx.signals, ctx, sc.params)
        for s, reason in results:
            if reason and not any(s is item["signal"] for item in suppressed):
                suppressed.append({"signal": s, "reason": reason, "suppressor": sc.name})
    ctx.suppressed = suppressed
    ctx.signals = [s for s in ctx.signals if not any(s is item["signal"] for item in suppressed)]
```

### 6. `pipeline/l1_detect.py`
```python
async def l1_detect(ctx) -> None:
    for dc in ctx.domain_config.detectors:            # DetectorSpec
        detector = ctx.registry.get("detector", dc.plugin)
        matched = filter_signals(ctx.signals, dc.signal)
        if not matched:
            continue
        try:
            detected = await detector.detect(matched, dc.params)
        except Exception as exc:                       # 单 detector 失败隔离，不拖垮整轮（M4 遗留 HIGH）
            logger.warning("detector failed plugin=%s err=%s", dc.plugin, exc)
            continue
        for a in detected:
            a.method = detector.name
            a.severity = dc.severity                  # spec.severity 权威覆盖（骨架 a.severity = dc.get("severity", a.severity)）
            ctx.anomalies.append(a)
```
> 单个 detector 异常隔离：`try/except` 包 detect，记 `logger.warning` 不拖垮整轮（M4 遗留 HIGH）。

### 7. `pipeline/l2_correlate.py`
- 返回 `dict[service -> (Correlation, change_related: bool, recent_change: dict | None)]`（骨架是 2 元组；加 recent_change 满足 UC-5.5）。
- `_within_window(metric_anoms, log_anoms, window_sec)`：任意 metric 与 log anomaly 的 `detected_at` 时差 ≤ window → True（默认 300s）。
- `_change_within_window(changes, anoms, window_sec) -> (bool, dict|None)`：`changes`（ChangeSignal）中 service 匹配且 `abs(change.timestamp - a.detected_at) ≤ window` → `(True, {"change_id","summary","changed_at"})`。
- `template_summary(metric_anoms, log_anoms) -> str`：骨架模板——metric 拼 `"{service} {metric} {value}"`，log 拼 `"{service} {signature} x{count}"`，`"；"` 连接。
- reason 语义：同 service + 窗口内 → `metric_log_within_window`；仅 metric → `metric_only`；仅 log → `log_only`；两者无关 → `unrelated`。

### 8. `pipeline/l3_verify.py`
```python
async def l3_verify(ctx, service, anomalies) -> Verification:
    vc = ctx.domain_config.verify                       # VerifySpec
    persistence_rounds = vc.persistence_rounds
    fpr_threshold = vc.false_positive_threshold
    min_samples = vc.min_samples
    persisted = []
    for a in anomalies:
        key = fingerprint.anomaly_key(a)
        ctx.seen_keys.add(key)
        state = await ctx.state_store.get(ctx.tenant_id, ctx.domain, key)
        consecutive = state["consecutive_rounds"] if state else 0
        first_seen = state["first_seen"] if state else ctx.now
        if consecutive >= persistence_rounds:
            persisted.append(a)
        await ctx.state_store.upsert(ctx.tenant_id, ctx.domain, key,
            consecutive_rounds=consecutive + 1, miss_rounds=0,
            first_seen=first_seen, last_seen=ctx.now)
    if not persisted:
        return Verification(passed=False, persistence_ok=False, resample_ok=True,
                            false_positive_rate=0.0, final_severity="warning")
    gk = fingerprint.group_key(ctx.tenant_id, ctx.domain, service, persisted)
    entry = ctx.fpr.get(gk, {"fpr": 0.0, "total": 0})
    fpr = float(entry.get("fpr", 0.0)); total = int(entry.get("total", 0))
    fpr_ok = total < min_samples or fpr < fpr_threshold
    severity = "warning" if not fpr_ok else calibrate_severity(persisted)   # P0#8 降级不丢弃
    return Verification(passed=True, persistence_ok=True, resample_ok=True,
                        false_positive_rate=fpr, final_severity=severity)
```
- `calibrate_severity(anomalies)`：取最高 severity（warning<high<critical）；**组合升 critical 留 M6**（用例 2）。
- 持续性用 **per-key consecutive_rounds**（Enhanced plan P0#2 修正），**不用**设计文档 §6.6 的 `previous_keys` 交集法（已废弃）。

### 9. `pipeline/emit.py`
```python
async def emit(ctx, service, anomalies, correlation, change_related, recent_change, verification) -> list:
    if not verification.passed:
        return []
    metric_anoms = [a for a in anomalies if a.kind == "metric"]
    log_anoms = [a for a in anomalies if a.kind == "log"]
    evidence: list[dict] = []
    if ctx.degraded_sources:                            # UC-5.10 degraded 标记
        evidence.append({"type": "degraded", "target_ids": ctx.degraded_sources})
    rec = ProblemRecord(
        record_id=await ctx.sequence_store.next_id(ctx.domain),
        tenant_id=ctx.tenant_id, domain=ctx.domain, state="pending", service=service,
        severity=verification.final_severity,
        detected_at=ctx.now, first_seen_at=ctx.now, last_seen_at=ctx.now, occurrence_count=1,
        symptom={"summary": template_summary(metric_anoms, log_anoms)},
        metric_anomalies=metric_anoms, log_anomalies=log_anoms,
        correlation=correlation, change_related=change_related, recent_change=recent_change,
        verification=verification, evidence=evidence, trace_id=ctx.trace_id,
    )
    await ctx.storage.write_or_append(ctx.tenant_id, rec)   # 原子去重（M2 实现，返回 None）
    return [rec]
```
> **偏离骨架**：骨架 `actual_id = write_or_append(...)` 比 id 判重——M2 的 `write_or_append` 已原子去重且返回 `None`，故 emit 直接调用返回 `[rec]`（去重语义由 store 保证，见 UC-5.4「只有 1 条 record」）。

### 10. `pipeline/runner.py`
```python
async def run_domain(ctx) -> DomainResult:
    ctx.round_started_at = ctx.now
    timeline = [{"step": "collect_done", "ts": ctx.now, "count": len(ctx.signals)}]
    await l0_suppress(ctx);  timeline.append({"step": "suppressed", "count": len(ctx.suppressed)})
    await l1_detect(ctx);    timeline.append({"step": "detected", "count": len(ctx.anomalies)})
    correlations = await l2_correlate(ctx)
    timeline.append({"step": "correlated", "services": list(correlations)})
    by_service: dict[str, list] = defaultdict(list)
    for a in ctx.anomalies: by_service[a.service].append(a)
    records = []
    for service, anoms in by_service.items():
        corr, change_related, recent_change = correlations[service]
        verification = await l3_verify(ctx, service, anoms)
        records.extend(await emit(ctx, service, anoms, corr, change_related, recent_change, verification))
    await ctx.state_store.sweep(ctx.tenant_id, ctx.domain, ctx.seen_keys)   # miss 计数
    timeline.append({"step": "record_created", "count": len(records)})
    return DomainResult(domain=ctx.domain, records=records,
        suppressed_count=len(ctx.suppressed), anomaly_count=len(ctx.anomalies),
        degraded_sources=list(ctx.degraded_sources), timeline=timeline)
```
- **无信号提前终止**（UC-5.9）：anomalies 空 → by_service 空 → 无 L3/emit → records 空；timeline 记 collect_done count=0（零 LLM 天然满足，无 LLM 调用点）。

### 11. `storage/__init__.py`
- `Storage` 加 `sequence: SequenceStore`、`detection_state: DetectionStateStore`、`dynamic_config: DynamicConfigStore`；`build_storage` memory→InMemory 三件套，mysql→MySQL 三件套。

## 测试（TDD，先写测试再实现）

| 测试文件 | 覆盖 |
|---------|------|
| `test_sequence.py` | next_id 返回 `PR-YYYYMMDD-0001`；同日期递增 0002…；跨日期归 1；格式 `%04d` |
| `test_detection_state.py` | get 无返回 None；upsert 后 get 反映 consecutive/miss/first_seen/last_seen；sweep 未见 key miss+1/consecutive 归 0、见到的 key 不动；tenant/domain 隔离 |
| `test_dynamic_config.py` | 四类读取（InMemory 预置行）；blacklist 只取 enabled=1；fpr dict 形态 |
| `test_l2.py` | `_within_window` 窗口内/外；`_change_within_window` 命中（返回 change_id/summary）/未命中/跨 service 不命中；`template_summary` metric/log 拼串 |
| `test_l3.py` | `calibrate_severity` 取最高；fpr_ok（total<min_samples、fpr<threshold）/ 不 ok → 降级 warning 但 passed=True；persistence 门（consecutive≥2 才 persisted）；无 persisted → passed=False |
| `test_pipeline.py` | **§13/UC-5.x 端到端**（`build_context` + 真实 registry entry_points + InMemoryStorage + mock collector 信号预填）：UC-5.1 两轮 CPU、UC-5.3 47 条 OOM 聚合、UC-5.4 同源 1 条、UC-5.5 change 关联、UC-5.6 三轮抖动、UC-5.7 维护窗口、UC-5.8 fpr 降级、UC-5.9 无信号、UC-5.10 degraded、UC-5.11 弱信号 |

> 单轮场景（UC-5.3/5.4/5.5/5.7/5.8/5.10）测试配 `verify.persistence_rounds=1` 或跑两轮——按场景在 build_context 传对应 `DomainConfig`。

## 验证（完成标准）

1. `make lint` — ruff + mypy 全绿（新代码含 `# type: ignore[import-untyped]` 处理 aiomysql 处）。
2. `make test` — 全量 pytest 绿（原 182 + M5 新增用例）。
3. 完成标准复核：§13 用例 1/3/4/5/6/7/8/9/10/11 全通过（`test_pipeline.py` 端到端 + `test_l2`/`test_l3` 分支）。用例 2 端到端留 M6。
4. 手动冒烟（可选）：`make dev` 后 registry/ready 不回归（M5 不改 router/app）；M6 起补 `/v1/problems`、调度器。

## 文档同步（CLAUDE.md 流程）

1. 本文档落库 `docs/plans/M5-implementation-plan.md`，状态「进行中」。
2. 完成后写 `docs/logs/M5.md` 实现日志（改动点、文件清单、完成状态、遗留问题）。
3. 归档已实现章节到 `docs/archive/M5-funnel.md`；设计文档摘除 M5 已实现部分。
4. 更新 `README.md` 进度表（M5 → 已完成）。
5. 更新 `CLAUDE.md`「当前里程碑」为「M5 已完成，下一阶段 M6 调度/API/恢复闭环」。
6. 提交 M5（`[huhao] feat: ...`，无 Co-Authored-By）。
