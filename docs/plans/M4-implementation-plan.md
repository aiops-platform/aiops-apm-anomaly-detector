# M4 检测层（插件 registry + 内置 detector/suppressor）— 实现计划

> 状态：**已实现**（2026-08-26）。实现日志见 `docs/logs/M4.md`，历史规格归档见 `docs/archive/M4-plugins.md`。

## Context（为什么做）

- **前置**：M0–M3 已完成（`make lint test dev` 全绿，113 用例，M3 已提交 `1671007`）。M3 已交付共享出站 http/pool、`signature()` 纯函数、collectors entry_points（放开但未消费）。
- **M4 是什么**：真插件系统可用——`PluginRegistry` 通过三个 `entry_points` group 动态发现插件；内置 3 个 detector（`static_threshold` / `simple_compare` / `signature_aggregate`）+ 2 个 suppressor（`maintenance_window` / `blacklist`）全部实现；`filter_signals` 结构化 matcher 落地（L1 分发用）。
- **整体位置**：M4 交付「可插拔规则」原则（设计原则 #2）的 registry 与内置插件；M5 漏斗（`l0_suppress`/`l1_detect`/`l2`/`l3`/emit）通过 `ctx.registry.get(kind, name)` 消费这些插件。
- **完成标准**（设计原文）：插件契约测试全通过；`reload` 期间跑一轮不抛异常；`filter_signals` 结构化 matcher 全分支覆盖。
- **用户已确认的决策**：
  1. **M4 包含 `/v1/plugins` API**（GET 列表 + POST `/reload`），完成 UC-4.1/4.2；registry 接线进 app lifespan，`/ready` 的 plugins 变为 True。
  2. 内置 suppressor 的数据从 `ctx` 读（`ctx.maintenance_windows` / `ctx.blacklist`，与 Enhanced plan 骨架一致）；`maintenance_window` / `suppress_blacklist` 表的读取加载进 ctx 属 M5（DetectionContext 构建），写表的 admin API 属 M6。

## 改动点：位置与用途

```
src/aiops_apm/
├── plugins/registry.py                       # 新增：PluginRegistry（entry_points 发现 + MappingProxyType 原子快照）
├── detectors/
│   ├── __init__.py                           # 新增：包 + 公共符号导出
│   ├── static_threshold.py                   # 新增：Operator 枚举 + StaticThresholdDetector + build()
│   ├── simple_compare.py                     # 新增：SimpleCompareDetector + build()
│   └── signature_aggregate.py                # 新增：SignatureAggregateDetector + build()（复用 M3 signature()）
├── suppressors/
│   ├── __init__.py                           # 新增：包 + 公共符号导出
│   ├── maintenance_window.py                 # 新增：MaintenanceWindowSuppressor + build()
│   └── blacklist.py                          # 新增：BlacklistSuppressor + build()
├── pipeline/
│   ├── __init__.py                           # 新增：M5 漏斗的包起点
│   └── filter_signals.py                     # 新增：filter_signals 结构化 matcher
├── router/
│   ├── plugins.py                            # 新增：GET /v1/plugins + POST /v1/plugins/reload
│   └── api.py                                # 改：include plugins router
└── _app.py                                   # 改：lifespan 加载 registry → app.state.registry
pyproject.toml                                # 改：放开 detectors/suppressors entry_points
tests/                                        # 新增 5 个测试文件 + 更新 test_health.py
docs/plans/M4-implementation-plan.md          # 本文档（状态 进行中 → 已实现）
docs/logs/M4.md                               # 实现日志
docs/archive/M4-plugins.md                    # 归档已实现章节
README.md / CLAUDE.md                         # 进度同步
```

**消费关系**（M4 交付后，M5 漏斗消费）：

```
l0_suppress(ctx):  for sc in ctx.domain_config.suppressors:
                        sup = ctx.registry.get("suppressor", sc.name)
                        reason = await sup.check(s, ctx, sc.params)      # maintenance_window / blacklist
l1_detect(ctx):    for dc in ctx.domain_config.detectors:
                        detector = ctx.registry.get("detector", dc.plugin)
                        matched = filter_signals(ctx.signals, dc.signal)  # 结构化 matcher
                        anomalies += await detector.detect(matched, dc.params)
```

## 范围

### 交付

| 交付项 | 说明 |
|--------|------|
| PluginRegistry | `load` / `reload` / `get` / `list` / `register`；entry_points 发现；MappingProxyType 原子快照；单插件失败隔离 |
| 3 个内置 detector | `static_threshold`（Operator GT/GTE/LT/LTE/RANGE）、`simple_compare`（ratio+baseline）、`signature_aggregate`（signature 分组聚合） |
| 2 个内置 suppressor | `maintenance_window`（ctx.maintenance_windows）、`blacklist`（ctx.blacklist） |
| `filter_signals` | 结构化 matcher（str/dict/`*`/None 全分支） |
| `/v1/plugins` API | GET 列表（UC-4.1）+ POST `/reload`（UC-4.2） |
| `/ready` | plugins: True（registry 接线） |
| pyproject | detectors/suppressors entry_points 放开 |

### 不做（明确排除）
- **`l0_suppress` / `l1_detect` 漏斗主体**——M5（pipeline/runner、context、l2、l3、emit）。M4 只交付 registry + 插件 + filter_signals 纯函数。
- **`maintenance_window` / `suppress_blacklist` 表读取**（把表数据加载进 ctx）——M5 DetectionContext；**admin 写这些表的 API**——M6。
- **simple_compare 从 signal_snapshot 取滚动均值基线**——Enhanced plan 骨架注释掉的 stub；基线从 `params["baseline"]` 取，滚动均值由 M5 pipeline 注入 ctx 后进化。
- **`POST /v1/plugins/reload` 的 admin 权限校验**——当前无认证体系，M7 安全加固。
- **前端插件管理页**（PluginListPage / PluginTable）——M0 明确不做前端；仅后端 API。
- **第三方插件包**（examples/custom_detector）——设计 §5.4 演示性内容，M4 用 registry 单测（`register` + reload 发现）覆盖 UC-4.8 语义。

## 文件清单（新增）

```
src/aiops_apm/plugins/registry.py
src/aiops_apm/detectors/__init__.py
src/aiops_apm/detectors/static_threshold.py
src/aiops_apm/detectors/simple_compare.py
src/aiops_apm/detectors/signature_aggregate.py
src/aiops_apm/suppressors/__init__.py
src/aiops_apm/suppressors/maintenance_window.py
src/aiops_apm/suppressors/blacklist.py
src/aiops_apm/pipeline/__init__.py
src/aiops_apm/pipeline/filter_signals.py
src/aiops_apm/router/plugins.py
tests/test_registry.py
tests/test_detectors.py
tests/test_suppressors.py
tests/test_filter_signals.py
tests/test_plugins_api.py
```

## 关键实现细节

### 1. `plugins/registry.py` — PluginRegistry（设计 §5.3 + Enhanced plan 骨架）
```python
import importlib.metadata as m
from types import MappingProxyType
from aiops_apm.plugins.base import Plugin

GROUPS = {
    "collector":  "aiops_apm.collectors",
    "detector":   "aiops_apm.detectors",
    "suppressor": "aiops_apm.suppressors",   # 单数，与设计 §5.3 / l0_suppress 一致（Enhanced plan 的 "suppressors" 是笔误）
}

class PluginRegistry:
    def __init__(self) -> None:
        self._active: MappingProxyType[str, dict[str, Plugin]] = MappingProxyType({k: {} for k in GROUPS})

    def load(self, *, http=None, pool=None, settings=None) -> "PluginRegistry":
        """构建新快照（遍历三组 entry_points，build(http=..., pool=..., settings=...)），原子替换。"""
        snapshot = {k: {} for k in GROUPS}
        for kind, group in GROUPS.items():
            for ep in m.entry_points(group=group):
                try:
                    factory = ep.load()
                    plugin = factory(http=http, pool=pool, settings=settings)
                    snapshot[kind][ep.name] = plugin
                except Exception as exc:  # 单插件失败不拖垮整体
                    logger.warning("plugin load failed", group=group, name=ep.name, err=exc)
        self._active = MappingProxyType(snapshot)
        return self

    def reload(self, *, http=None, pool=None, settings=None) -> "PluginRegistry":
        """重新发现插件（重新扫 entry_points），构建新快照后一次原子替换。"""
        return self.load(http=http, pool=pool, settings=settings)

    def register(self, kind: str, name: str, plugin: Plugin) -> None:
        """注入插件（design §5.3；reload 发现新插件的测试钩子/管理入口）。"""
        snapshot = {k: dict(v) for k, v in self._active.items()}
        snapshot.setdefault(kind, {})[name] = plugin
        self._active = MappingProxyType(snapshot)

    def get(self, kind: str, name: str) -> Plugin:
        table = self._active.get(kind, {})
        if name not in table:
            raise AppException(ErrorCode.PLUGIN_NOT_FOUND, f"{kind}/{name}")
        return table[name]

    def list(self, kind: str | None = None) -> dict:
        if kind:
            return {kind: list(self._active.get(kind, {}).keys())}
        return {k: list(v.keys()) for k, v in self._active.items()}
```
> `ErrorCode.PLUGIN_NOT_FOUND` 已存在（exceptions.py）。registry 无状态、无依赖，`get` 未命中抛 AppException → 统一异常响应 500（`_status_for_code` 未映射 PLUGIN_NOT_FOUND；M5 漏斗内部捕获用，非 API 直出）。

### 2. `detectors/static_threshold.py`（Enhanced plan 骨架）
- `Operator(str, Enum)`：`GT/GTE/LT/LTE/RANGE`。
- `StaticThresholdDetector(Detector)`，`name = "static_threshold"`：
  - 只处理 `MetricSignal`，其余跳过。
  - `operator = Operator(params.get("operator", "gt"))`；`threshold = params["threshold"]`。
  - RANGE：`lo, hi = params.get("range", [threshold, threshold])`，`hit = not (lo <= s.value <= hi)`。
  - 命中 → `MetricAnomaly(kind="metric", tenant_id=s.tenant_id, service=s.service, metric=s.metric, value=s.value, method=self.name, severity=params.get("severity", "warning"), detected_at=s.timestamp, labels=s.labels)`。

### 3. `detectors/simple_compare.py`
- `SimpleCompareDetector(Detector)`，`name = "simple_compare"`。
- `ratio = params.get("ratio", 1.5)`；`baseline = params.get("baseline")`。
- `if baseline is not None and s.value > baseline * ratio:` → `MetricAnomaly(..., baseline=baseline, method=self.name, severity=params.get("severity", "warning"), detected_at=s.timestamp)`（不带 labels——骨架如此）。

### 4. `detectors/signature_aggregate.py`
- `SignatureAggregateDetector(Detector)`，`name = "signature_aggregate"`。
- `min_count = params.get("min_count", 5)`；`n_frames = params.get("n_frames", 3)`。
- 只处理 `LogSignal`；`sig = s.signature or signature(s, n_frames)`（优先用 M3 预计算值，回退纯函数）。
- 按 signature 分组；`len(logs) >= min_count` → `LogAnomaly(kind="log", tenant_id=logs[0].tenant_id, service=logs[0].service, level=logs[0].level, signature=sig, pattern=logs[0].message[:120], count=len(logs), first_seen=min(l.timestamp for l in logs), severity=params.get("severity", "warning"), detected_at=max(l.timestamp for l in logs))`。

### 5. `suppressors/maintenance_window.py` + `blacklist.py`（Enhanced plan 骨架）
- `MaintenanceWindowSuppressor`，`name = "maintenance_window"`：`check(signal, ctx, params)` 遍历 `ctx.maintenance_windows`（list of `{service, start_at, end_at, reason}`），`w["service"]==signal.service and w["start_at"] <= signal.timestamp <= w["end_at"]` → `f"maintenance_window: {w.get('reason','')}"`；`batch_check` 一次循环返回 `(s, reason)` 列表。
- `BlacklistSuppressor`，`name = "blacklist"`：`batch_check` 遍历 `ctx.blacklist`（list of `{domain, service, signal, reason}`），service 匹配 +（MetricSignal 且 `entry["signal"]==s.metric`）或（LogSignal 且 `entry["signal"]==s.level`）→ `f"blacklist: {entry.get('reason','')}"`。
- 两者 `build()` 返回无状态实例；**不接触表**——数据由调用方经 ctx 提供（M5 DetectionContext 从表加载）。

### 6. `pipeline/filter_signals.py`（Enhanced plan 骨架，完成标准点名）
```python
def filter_signals(signals: list, matcher: str | dict | None) -> list:
    if matcher is None or matcher == "*":
        return signals
    if isinstance(matcher, str):                       # 向后兼容：metric 名或 log level
        return [s for s in signals
                if (isinstance(s, MetricSignal) and s.metric == matcher)
                or (isinstance(s, LogSignal) and s.level == matcher)]
    if isinstance(matcher, dict):
        out = []
        for s in signals:
            if matcher.get("signal_type") == "metric" and isinstance(s, MetricSignal):
                if (not matcher.get("metric") or s.metric == matcher["metric"]) \
                   and _labels_match(s.labels, matcher.get("labels", {})) \
                   and (not matcher.get("service") or s.service == matcher["service"]):
                    out.append(s)
            elif matcher.get("signal_type") == "log" and isinstance(s, LogSignal):
                if (not matcher.get("level") or s.level == matcher["level"]) \
                   and (not matcher.get("service") or s.service == matcher["service"]):
                    out.append(s)
        return out
    return []

def _labels_match(actual: dict, wanted: dict) -> bool:
    return all(actual.get(k) == v for k, v in wanted.items())
```
> 语义：dict matcher 的 `signal_type` 是分派键（metric→metric/labels/service；log→level/service）；空值/缺失视为通配。

### 7. `router/plugins.py`（用户确认纳入）
- `GET /v1/plugins` → `request.app.state.registry.list()`（`{"collector":[...],"detector":[...],"suppressor":[...]}`）。
- `POST /v1/plugins/reload` → `registry.reload(http=app.state.http_client, pool=getattr(app.state.storage, "pool", None), settings=app.state.settings)` → 返回更新后的 `list()`。
- 挂 `prefix="/v1/plugins"` 到 `api_router`；registry 缺省时 `raise AppException(ErrorCode.INTERNAL, "registry not loaded")`。

### 8. `_app.py` lifespan 接线
- 在 `app.state.http_client` 之后：`app.state.registry = PluginRegistry().load(http=app.state.http_client, pool=getattr(app.state.storage, "pool", None), settings=settings)`。
- `api.py::ready` 已读 `bool(getattr(state, "registry", None))` → M4 后 plugins 变 True，`/ready` 200。
- memory backend + 已安装包：lifespan 正常加载 8 个插件（collector `build()` 构造实例无副作用）。

### 9. `pyproject.toml`
- 放开 `[project.entry-points."aiops_apm.detectors"]`（static_threshold / simple_compare / signature_aggregate）与 `[project.entry-points."aiops_apm.suppressors"]`（maintenance_window / blacklist）。
- **改后必须 `.venv/bin/pip install -e ".[dev]"` 刷新 entry_points**（editable install 不会自动重新注册新 entry_points）。

## 测试（TDD，先写测试再实现）

| 测试文件 | 覆盖 | 断言 |
|---------|------|------|
| `test_registry.py` | 5.3/UC-4.1/4.2/4.8 | `load()` 发现 3 collector + 3 detector + 2 suppressor（真 entry_points）；`get` 命中/未命中抛 `PLUGIN_NOT_FOUND`；`list` 分组/过滤；`register` 注入 + `reload` 后新插件在列；reload 原子替换（旧实例仍可用，`is not` 新实例，reload 期间跑一轮 detect 不抛异常）；单插件 build 失败隔离（其余正常加载）；MappingProxyType 不可直接改 |
| `test_detectors.py` | 6.4 三插件 | static_threshold：GT/GTE/LT/LTE/RANGE 五算子 + RANGE 区间内不命中 + 跳过非 MetricSignal + severity 默认 warning + labels 透传 + method=插件名；simple_compare：ratio 生效、baseline 来自 params、无 baseline 不命中；signature_aggregate：47 条同签名 → 1 条 count=47、低于 min_count 不命中、优先用预计算 `s.signature`、跳过非 LogSignal、first_seen=min/detected_at=max |
| `test_suppressors.py` | 6.3 两插件 | maintenance_window：窗口内抑制（reason 含维护原因）、窗口外放行、service 不匹配放行、batch_check 返回 (s, reason) 元组；blacklist：metric 命中/log level 命中/service 不匹配放行/reason、batch_check |
| `test_filter_signals.py` | 完成标准点名 | 全分支：None 全量、`*` 全量、str metric、str log、str 不匹配、dict metric（无 metric 键通配 / metric 匹配 / metric 不匹配 / labels 匹配与不匹配 / service 匹配与不匹配）、dict log（无 level 通配 / level 匹配 / service）、dict 错误 signal_type 返回空、非 str/dict/None 返回空 |
| `test_plugins_api.py` | UC-4.1/4.2 | `GET /v1/plugins` 200 → 3/3/2 分组；`POST /v1/plugins/reload` 200 → 更新后列表；用 conftest `client`（memory + lifespan 已加载 registry） |
| `test_health.py`（改） | UC-0.1 | `test_ready_not_ready_without_plugins` → 改为 `test_ready_ok_with_plugins`：memory backend db:True + plugins:True → 200 `{"status":"ready"}` |

## 验证（完成标准）

1. `make lint` — ruff + mypy 全绿（新代码含 `# type: ignore[import-untyped]` 处理 importlib.metadata 无 stub 处）。
2. `make test` — 全量 pytest 绿（原 113 + 新增用例）。
3. 完成标准复核：
   - 插件契约测试全通过（test_registry + test_detectors + test_suppressors 全绿）；
   - `reload` 期间跑一轮不抛异常（test_registry 并发原子快照用例）；
   - `filter_signals` 结构化 matcher 全分支覆盖（test_filter_signals）。
4. 手动 API 冒烟（`make dev` + curl）：
   - `GET /v1/plugins` → `{"collector":["http_logs","http_metrics","mock"],"detector":["signature_aggregate","simple_compare","static_threshold"],"suppressor":["blacklist","maintenance_window"]}`。
   - `POST /v1/plugins/reload` → 200 更新后列表。
   - `GET /ready` → 200 `{"status":"ready","checks":{"db":true,"plugins":true}}`。

## 文档同步（CLAUDE.md 流程）

1. 本文档落库，状态「进行中」。
2. 完成后写 `docs/logs/M4.md` 实现日志（改动点、文件清单、完成状态、遗留问题）。
3. 归档已实现章节到 `docs/archive/M4-plugins.md`；从设计文档摘除 M4 已实现部分。
4. 更新 `README.md` 进度表（M4 → 已完成；新增 /v1/plugins 用法 + /ready plugins:True）。
5. 更新 `CLAUDE.md`「当前里程碑」为「M4 检测层已完成，下一阶段 M5 漏斗」。
6. 最后提交 M4（feat，`[huhao] feat: ...`，无 Co-Authored-By）。
