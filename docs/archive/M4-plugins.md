# M4 插件化（registry + 内置 detector/suppressor）— 历史规格归档

> 本文归档 `docs/apm-alert-module-design.md`（§5 插件系统、§6.3 L0 抑制、§6.4 L1 检测）与 `docs/apm-alert-implementation-plan-enhanced.md`（M4 小节）中**已实现**的部分。实现日志见 [`docs/logs/M4.md`](../logs/M4.md)，实现计划见 [`docs/plans/M4-implementation-plan.md`](../plans/M4-implementation-plan.md)。

## 目标

真插件系统可用：`PluginRegistry` 通过三个 `entry_points` group 动态发现插件（设计原则 #2「可插拔规则」落地）；内置 3 个 detector + 2 个 suppressor 全部实现；`filter_signals` 结构化 matcher 落地（L1 分发用）。M5 漏斗通过 `ctx.registry.get(kind, name)` 消费这些插件。

## 三个插件组（设计 §5.1/5.3，M4 落地 registry）

| 插件组 | entry_points group | 职责 | 插入点 |
|--------|-------------------|------|--------|
| Collector | `aiops_apm.collectors` | 从第三方 API 拉取信号 | collect 阶段（L0 之前，M3 实现） |
| Detector | `aiops_apm.detectors` | 检测规则（阈值/环比/签名…） | L1 阶段（M4 实现） |
| Suppressor | `aiops_apm.suppressors` | 抑制规则（维护窗口/黑名单…） | L0 阶段（M4 实现） |

内置插件通过 `pyproject.toml` 声明，entry 指向 `build() -> Plugin` 工厂函数（而非类），插件名以 entry_points 名为准：

```toml
[project.entry-points."aiops_apm.collectors"]
http_metrics = "aiops_apm.collectors.http_metrics:build"   # M3
http_logs    = "aiops_apm.collectors.http_logs:build"
mock         = "aiops_apm.collectors.mock:build"
[project.entry-points."aiops_apm.detectors"]               # M4
static_threshold    = "aiops_apm.detectors.static_threshold:build"
simple_compare      = "aiops_apm.detectors.simple_compare:build"
signature_aggregate = "aiops_apm.detectors.signature_aggregate:build"
[project.entry-points."aiops_apm.suppressors"]             # M4
maintenance_window = "aiops_apm.suppressors.maintenance_window:build"
blacklist          = "aiops_apm.suppressors.blacklist:build"
```

## PluginRegistry（设计 §5.3，M4 落地 `plugins/registry.py`）

- `load(*, http=None, pool=None, settings=None)` — 遍历三组 entry_points，`factory(http=..., pool=..., settings=...)` 实例化；构建**新快照**后一次 `MappingProxyType` 原子替换。单插件加载失败 `logger.warning` 隔离不拖垮整体。
- `reload(*, ...)` — 重新发现插件（重新扫 entry_points），原子替换为新快照；正在执行的轮次继续用旧快照。
- `get(kind, name)` — 未命中抛 `AppException(ErrorCode.PLUGIN_NOT_FOUND, f"{kind}/{name}")`。
- `list(kind=None)` — 返回 `{kind: [插件名...]}`，供 `/v1/plugins`。
- `register(kind, name, plugin)` — 注入插件（design §5.3；测试/管理入口，reload 会覆盖）。
- 防御：第三方 `async build()`（契约要求同步）→ `inspect.iscoroutine` 拒绝并告警。

## 内置 detector（设计 §6.4，M4 落地 `detectors/`）

| 插件 | 信号类型 | 逻辑 | 关键参数 |
|------|---------|------|---------|
| `static_threshold` | 指标 | `value` 按 `operator` 越过 `threshold` → MetricAnomaly | `threshold`, `operator`（gt/gte/lt/lte/range），RANGE=区间外命中 |
| `simple_compare` | 指标 | `value > baseline * ratio`（基线来自 params）→ MetricAnomaly | `baseline`, `ratio`（默认 1.5） |
| `signature_aggregate` | 日志 | 按堆栈签名分组，`count >= min_count` → 1 条 LogAnomaly(count) | `min_count`, `n_frames` |

**签名**（`signature(log, n_frames=3)` 纯函数，M3 已落地 `src/aiops_apm/signature.py`）：`异常类型|顶部N帧（去行号）`；无堆栈回退 `message[:120]`。`signature_aggregate` 优先用采集器预计算的 `LogSignal.signature`，缺省回退纯函数。

> **v1 检测方法限定**：`static_threshold` + `simple_compare` + `signature_aggregate`；`dynamic_baseline`（zscore）/ ML 基线留 V2 接入。

## 内置 suppressor（设计 §6.3，M4 落地 `suppressors/`）

| 插件 | 逻辑 | 数据源 |
|------|------|--------|
| `maintenance_window` | 信号 `timestamp` 落在维护窗口内（service 匹配）→ 抑制 | `ctx.maintenance_windows`（M5 从表加载） |
| `blacklist` | 命中 `service + signal`（MetricSignal.metric / LogSignal.level）→ 抑制 | `ctx.blacklist`（M5 从表加载） |

M4 插件只消费 ctx 提供的列表（纯函数）；`maintenance_window` / `suppress_blacklist` 表读取加载进 ctx 属 M5（DetectionContext），写表 admin API 属 M6。

## filter_signals 结构化 matcher（Enhanced plan M4 骨架，M4 落地 `pipeline/filter_signals.py`）

```
* / None / ""      → 全量
str                → 向后兼容：MetricSignal.metric == matcher 或 LogSignal.level == matcher
dict               → signal_type 分派：
                       metric → metric（缺失=通配）+ labels（子集匹配）+ service
                       log    → level（缺失=通配）+ service
其余类型           → []
```

M5 `l1_detect` 用它对每个 DetectorSpec.signal 分发信号：`matched = filter_signals(ctx.signals, dc.get("signal"))`，对 `matched` 执行 `detector.detect(matched, dc.params)`。

## /v1/plugins API（UC-4.1 / UC-4.2，M4 落地 `router/plugins.py`）

- `GET /v1/plugins` — 返回 `{"collector": [...], "detector": [...], "suppressor": [...]}`（内置 3/3/2）。
- `POST /v1/plugins/reload` — 重新扫 entry_points 原子替换快照，返回更新列表（`asyncio.to_thread` 放线程执行防阻塞；admin 权限校验留 M7）。
- registry 由 lifespan 接线进 `app.state.registry`；`/ready` 的 `plugins` 由 M4 起为 True。

## 范围（不做，留后续里程碑）

- **漏斗主体 `l0_suppress` / `l1_detect` / L2 / L3 / emit** — M5（pipeline/runner、context、l2、l3、emit）。
- **维护窗口 / 黑名单表读取**（加载进 ctx）— M5 DetectionContext；**admin 写表 API** — M6。
- **simple_compare 从 signal_snapshot 取滚动均值基线** — M5 pipeline 注入。
- **detector params 写入侧校验**（`ConfigValidationError`）— M6 config API（UC-4.3）。
- **`POST /v1/plugins/reload` admin 权限** — M7 安全加固。
- **前端插件管理页**（PluginListPage / PluginTable）— M0 明确不做前端。
- **第三方插件包示例**（examples/custom_detector，设计 §5.4）— registry 单测覆盖 UC-4.8 语义。
