# APM 告警模块 — 详细设计

> 范围：一个聚焦的 **APM 告警模块**（Python，开放 server），从第三方 API 采集日志/指标，经 **L0–L3 漏斗**，由**可插拔 + 手动配置**的规则引擎检测，最终产出**问题记录（problem_record）**落库。本文为独立设计文档，自包含、不依赖其他文档。

---

## 1. 定位与范围

| 项 | 内容 |
|----|------|
| **职责** | 从第三方 API（日志监控 + 指标监控）主动发现异常，经 L0–L3 漏斗过滤，生成 `problem_record` 落库 |
| **不做** | 根因诊断、修复、KB 沉淀（下游职责，本文只预留接口边界） |
| **输入** | 第三方 API 的指标流、日志流（可选：变更流） |
| **输出** | `problem_record`（`state=pending`） |
| **触发** | 定时调度（按监控端点 `schedule`，默认 60s）+ 手动触发（HTTP 端点，供调试/前端触发） |

**四个核心设计原则**（不可回退）：

1. **确定性优先**：检测是「确定性 pipeline + 少量 LLM」。LLM 只做现象摘要（L2 可选项），**绝不参与检测决策**；L1/L2/L3 全是确定性纯函数。
2. **可插拔规则**：规则（检测方法 / 采集源 / 抑制规则）通过 **entry_points 插件系统**动态加载，域配置在 **MySQL** 里用「插件名 + 参数」引用，**改配置即插拔**。
3. **单一 trace_id**：每轮检测一个 `trace_id` 贯穿采集→漏斗→落库，写入 `problem_record`，下游沿用做全链路追踪。
4. **多租户隔离**：全链路携带 `tenant_id`（请求头 `X-Tenant-Id`，默认 `default`），配置、调度、采集、落库、查询均按租户隔离，使本模块可复用于多套服务/业务线。

---

## 2. 关键设计决策

| # | 决策点 | 选择 | 理由 |
|---|--------|------|------|
| 1 | 编排骨架 | **简单确定性 asyncio pipeline** | 不用 LangGraph（StateGraph/checkpointer），用纯 `asyncio` 串起 collect→L0→L1→L2→L3→emit。轻量、易单测，后续要复杂编排再迁 |
| 2 | 持久化 | **MySQL 直连**（aiomysql） | 生产直接落库；不引入存储抽象层之外的过度封装 |
| 3 | 规则机制 | **真正 plugin 系统（entry_points 动态加载）** | 检测器/采集器/抑制器做成独立可分发插件，`pip install` 后自动被发现；改配置即插拔 |
| 4 | 配置承载 | **MySQL 承载（YAML 仅作 seed）** | 检测规则与监控端点均入库，运行时改库即生效，支持自服务新增监控端点 |
| 5 | 多租户 | **全链路 `tenant_id` 隔离** | 所有表、配置、调度、API 均带 `tenant_id`（请求头 `X-Tenant-Id`，默认 `default`），一套实例服务多个租户/业务线，互不可见 |

---

## 3. 总体架构

```
┌───────────────────────────────────────────────────────────────┐
│  APM 告警模块 (Python 3.10+, FastAPI, :8000)                    │
│                                                               │
│   ┌────────── 管理 API 层 ──────────────────────────────┐     │
│   │ GET  /health /ready                                 │     │
│   │ POST /v1/alerts/run        手动触发一轮检测          │     │
│   │ GET  /v1/problems          查询 problem_record       │     │
│   │ POST /v1/monitors          新增监控端点（自服务）    │     │
│   │ GET  /v1/monitors          列出监控端点              │     │
│   │ PUT/DELETE /v1/monitors/{id}  修改/删除监控端点      │     │
│   │ POST /v1/monitors/{id}/run 立即执行该端点检测        │     │
│   │ GET  /v1/plugins           列出已加载插件            │     │
│   │ POST /v1/plugins/reload    重新发现插件              │     │
│   └────────────────────────────────────────────────────┘     │
│                                                               │
│   ┌────────── 调度器 scheduler.py（按端点 schedule） ──────┐   │
│   └───────────────────────────────────────────────────────┘   │
│                                                               │
│   ┌────────── 确定性 Pipeline (asyncio) ─────────────────┐     │
│   │ collect → L0 suppress → L1 detect → L2 correlate     │     │
│   │        → L3 verify → emit                            │     │
│   └──────────────────────────────────────────────────────┘     │
│                                                               │
│   ┌────────── 插件系统 (entry_points) ───────────────────┐     │
│   │  Collector 插件 / Detector 插件 / Suppressor 插件      │     │
│   └──────────────────────────────────────────────────────┘     │
└───────────────────────────────────────────────────────────────┘
        │ HTTP (Collector 插件适配)          │ aiomysql 直连
        ▼                                   ▼
┌──────────────────────────┐      ┌─────────────────────────────┐
│ 第三方 API               │      │ MySQL (aiops_apm_runtime 库)     │
│ Prometheus / ELK / 任意  │      │  ├ problem_record            │
│ 指标 HTTP / 日志 HTTP     │      │  ├ change_record             │
└──────────────────────────┘      │  ├ monitor_target            │
                                  │  ├ domain_config             │
                                  │  ├ maintenance_window        │
                                  │  ├ suppress_blacklist        │
                                  │  └ fpr_table                 │
                                  └─────────────────────────────┘
```

> 所有表统一放在单一 schema `aiops_apm_runtime`（业务配置、输出与运行时/历史数据同库）。所有表均带 `tenant_id` 实现多租户隔离。

**一轮检测的数据流**：

```
scheduler(按端点 schedule) → 载入 monitor_target(端点) + domain_config(检测规则)，均按 tenant_id 过滤 → 生成 trace_id → run_round
  每轮: collect(按 target 并行采集) → L0 抑制 → L1 检测 → L2 关联 → L3 验证 → emit
  emit: 按 service 分组 → 组装 ProblemRecord(带 tenant_id) → 去重(find_open_record，租户内) → write/update
```

### 3.1 人驱动 vs Adapter 适配（谁在环外、谁在环内）

> 人只做「配置」与「反馈」（环外）；采集、检测、抑制、调度全部由 **Adapter（插件）自动适配**执行（环内）。改库即生效，反馈让下一轮更准。

图例：**[人]** 人驱动（配置 / 反馈）　　**[A]** Adapter 自动适配（插件 / 调度）

```text
┌──────────────────────────── 人驱动（Human-driven）────────────────────────────┐
│                                                                              │
│   ① 新增监控端点 [人]           ② 配置检测规则 [人]           ③ 维护窗口 [人] │
│      monitor_target                domain_config                 maintenance_ │
│      (service / signal_type        (detectors /                   window /    │
│       / source_config /             suppressors /                 blacklist   │
│       schedule)                     correlation / verify)                     │
│                                                                              │
└──────┬──────────────────────────────────┬───────────────────────────┬────────┘
       │                                  │                           │
       │        写入 MySQL（改库即生效，无需重启）                    │
       ▼                                  ▼                           ▼
┌────────────────────── Adapter 自动适配（Automated）───────────────────────────┐
│                                                                              │
│   scheduler [A] → collect [A] → L0 抑制 [A] → L1 检测 [A] → L2 关联 [A]       │
│   按端点 schedule     collector         suppressor         detector       关联  │
│   到期触发            适配 source_type      插件              插件      (指标+  │
│                      + field_mapping   (维护窗口/        (阈值/环比/   日志同源 │
│                      适配第三方 API     黑名单)          签名聚合)    /变更)   │
│                                                                              │
│   → L3 验证 [A] → emit 开单 [A] → problem_record → 下游处置 [人]              │
│      误报率闸门          去重             state=pending      pending→resolved  │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
       ▲                                                                │
       │                 ④ 误报判定回写 [人]（fpr_table）                │
       └────────────────────────────────────────────────────────────────┘

> 多租户：上述配置实体（`monitor_target` / `domain_config` / `maintenance_window` / `suppress_blacklist` / `fpr_table`）均带 `tenant_id`——人配置时绑定租户，Adapter 执行时按租户隔离，租户间互不可见。
```

---

## 4. 代码骨架（目录结构）

> 这是「骨架」的完整目录树。每一处都有明确的职责说明，后续按此实现。

```
apm-alert/
├── pyproject.toml                 # 依赖 + entry_points 声明（插件注册）
├── requirements.txt
├── .env.example                   # 环境变量样例（MySQL 连接、端口等）
├── README.md
│
├── migrations/
│   └── V1__init_tables.sql        # problem_record + monitor_target + change_record + 动态配置表
│
├── src/aiops_apm/
│   ├── __init__.py
│   ├── settings.py                # pydantic-settings（端口、MySQL、调度参数）
│   ├── exceptions.py              # ErrorCode + AppException
│   ├── _app.py                    # FastAPI 工厂 + lifespan（启动插件/scheduler）
│   │
│   ├── config/                    # ★ 配置加载
│   │   ├── loader.py              # DomainConfigLoader：域配置从 MySQL 加载（YAML 作 seed）
│   │   ├── domains.yaml           # 域配置 seed（首次初始化 / 无 DB 兜底）
│   │   └── harness.yaml           # 调度/超时/降级（静态）
│   │
│   ├── models/                    # ★ Pydantic 数据模型（信号/异常/记录）
│   │   ├── __init__.py
│   │   ├── signal.py              # MetricSignal / LogSignal / ChangeSignal
│   │   ├── anomaly.py             # MetricAnomaly / LogAnomaly
│   │   └── record.py              # Correlation / Verification / ProblemRecord
│   │
│   ├── plugins/                   # ★ 插件系统核心
│   │   ├── __init__.py
│   │   ├── base.py                # Collector / Detector / Suppressor 抽象基类
│   │   └── registry.py            # PluginRegistry（entry_points 发现 + 注册 + 重载）
│   │
│   ├── collectors/                # 内置 Collector 插件（经 entry_points 注册）
│   │   ├── __init__.py
│   │   ├── http_metrics.py        # 从第三方 HTTP 拉指标
│   │   ├── http_logs.py           # 从第三方 HTTP 拉日志
│   │   └── mock.py                # mock 采集器（demo/单测）
│   │
│   ├── detectors/                 # 内置 Detector 插件（L1 检测规则）
│   │   ├── __init__.py
│   │   ├── static_threshold.py    # 静态阈值
│   │   ├── simple_compare.py      # 简单环比（当前值 vs 基线）
│   │   └── signature_aggregate.py # 堆栈签名聚合
│   │
│   ├── suppressors/               # 内置 Suppressor 插件（L0 抑制规则）
│   │   ├── __init__.py
│   │   ├── maintenance_window.py  # 维护窗口抑制
│   │   └── blacklist.py           # 黑名单/静默
│   │
│   ├── pipeline/                  # ★ 确定性 pipeline
│   │   ├── __init__.py
│   │   ├── context.py             # DetectionContext / DomainResult / DetectionState
│   │   ├── runner.py              # 编排入口：run_round / run_target
│   │   ├── l0_suppress.py         # L0 抑制
│   │   ├── l1_detect.py           # L1 检测（dispatch detector 插件）
│   │   ├── l2_correlate.py        # L2 关联（指标+日志同源 / 变更关联 / 摘要）
│   │   ├── l3_verify.py           # L3 验证（持续性/误报率/严重度）
│   │   └── emit.py                # 组装 ProblemRecord + 去重落库
│   │
│   ├── storage/                   # ★ MySQL 直连
│   │   ├── __init__.py
│   │   ├── connection.py          # aiomysql 连接池
│   │   ├── records.py             # RecordStore（problem_record 读写/去重）
│   │   ├── monitor_target.py      # MonitorTargetStore（监控端点读写）
│   │   ├── domain_config.py       # DomainConfigStore（检测规则读写/seed）
│   │   └── dynamic_config.py      # DynamicConfigStore（维护窗口/黑名单/误报率）
│   │
│   ├── router/                    # 管理 API
│   │   ├── __init__.py
│   │   └── api.py                 # /health /ready /v1/alerts/run /v1/problems /v1/plugins /v1/monitors
│   │
│   ├── scheduler.py               # 按监控端点 schedule 调度（默认 60s）
│   └── poller.py                  # 单轮执行入口（被 scheduler 调用）
│
├── examples/
│   ├── mock_source.py             # 极简第三方 API 模拟器（供 http collector 演示）
│   ├── demo.py                    # 端到端演示：mock 采集 + 内存存储 + 跑一轮
│   └── custom_detector/           # ★ 第三方插件示例（独立 pip 包）
│       ├── pyproject.toml         # 声明 entry_points
│       ├── README.md
│       └── src/latency_detector/__init__.py
│
└── tests/
    ├── conftest.py
    └── test_pipeline.py           # L0-L3 纯函数 + 端到端用例
```

---

## 5. 插件系统设计（entry_points 真正可插拔）

### 5.1 三个插件组

| 插件组 | entry_points group | 职责 | 插入点 |
|--------|-------------------|------|--------|
| Collector | `aiops_apm.collectors` | 从第三方 API 拉取信号 | collect 阶段（L0 之前） |
| Detector | `aiops_apm.detectors` | 检测规则（阈值/环比/签名…） | L1 阶段 |
| Suppressor | `aiops_apm.suppressors` | 抑制规则（维护窗口/黑名单…） | L0 阶段 |

### 5.2 插件契约（抽象基类）

```python
# plugins/base.py —— 所有插件继承此契约
from abc import ABC, abstractmethod
from typing import ClassVar, Any

class Plugin(ABC):
    name: ClassVar[str] = ""          # 插件名（与 entry_points 名一致）

class Collector(Plugin):
    @abstractmethod
    async def collect(self, ctx: "DetectionContext", target: dict) -> list["Signal"]:
        """拉取一个监控端点（monitor_target 行）的信号，返回 Metric/Log/Change 信号列表。"""

class Detector(Plugin):
    @abstractmethod
    async def detect(self, signals: list, params: dict) -> list["Anomaly"]:
        """对匹配的信号执行检测，返回 MetricAnomaly/LogAnomaly 列表。"""

class Suppressor(Plugin):
    @abstractmethod
    async def check(self, signal: "Signal", ctx: "DetectionContext", params: dict) -> str | None:
        """返回抑制原因字符串（命中）或 None（放行）。"""
```

### 5.3 entry_points 声明与发现

内置插件通过 `pyproject.toml` 声明：

```toml
[project.entry-points."aiops_apm.collectors"]
http_metrics = "aiops_apm.collectors.http_metrics:build"
http_logs    = "aiops_apm.collectors.http_logs:build"
mock         = "aiops_apm.collectors.mock:build"

[project.entry-points."aiops_apm.detectors"]
static_threshold    = "aiops_apm.detectors.static_threshold:build"
simple_compare      = "aiops_apm.detectors.simple_compare:build"
signature_aggregate = "aiops_apm.detectors.signature_aggregate:build"

[project.entry-points."aiops_apm.suppressors"]
maintenance_window = "aiops_apm.suppressors.maintenance_window:build"
blacklist          = "aiops_apm.suppressors.blacklist:build"
```

**发现与注册**（`plugins/registry.py`）：

```python
import importlib.metadata as m

GROUPS = {
    "collector":  "aiops_apm.collectors",
    "detector":   "aiops_apm.detectors",
    "suppressor": "aiops_apm.suppressors",
}

class PluginRegistry:
    def __init__(self):
        self._plugins = {k: {} for k in GROUPS}

    def load(self):
        """启动时：遍历三个 group 的 entry_points，实例化并注册。"""
        for kind, group in GROUPS.items():
            for ep in m.entry_points(group=group):
                try:
                    factory = ep.load()          # 约定指向 build() 工厂函数
                    self.register(kind, ep.name, factory())
                except Exception as e:           # 单个插件失败不拖垮整体
                    log.warning("plugin load failed", group=group, name=ep.name, err=e)
        return self

    def register(self, kind, name, plugin): ...
    def get(self, kind, name): ...               # 未命中 → PluginNotFound
    def list(self, kind=None): ...               # 供 /v1/plugins
    def reload(self):                            # 重新发现（pick up 新安装的包）
        self._plugins = {k: {} for k in GROUPS}
        return self.load()
```

> **约定**：entry_points 值指向一个 `build() -> Plugin` 工厂函数（而非类），便于未来注入配置。插件名以 entry_points 名为准。

### 5.4 第三方插件（独立 pip 包）

演示「真正可插拔」：一个外部 detector 包，`pip install` 后无需改主程序代码即被 `PluginRegistry.load()` 发现，即可在域配置（`domain_config`）中引用。

```toml
# examples/custom_detector/pyproject.toml
[project]
name = "aiops-apm-latency-detector"
version = "0.1.0"
dependencies = ["aiops-apm"]

[project.entry-points."aiops_apm.detectors"]
p95_latency = "latency_detector:build"
```

```python
# examples/custom_detector/src/latency_detector/__init__.py
from aiops_apm.plugins.base import Detector
from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.signal import MetricSignal

class P95LatencyDetector(Detector):
    name = "p95_latency"
    async def detect(self, signals, params):
        threshold = params.get("threshold", 500)
        return [
            MetricAnomaly(service=s.service, metric=s.metric, value=s.value,
                          method=self.name, severity="high", detected_at=s.timestamp)
            for s in signals
            if isinstance(s, MetricSignal) and s.metric == "latency_p95" and s.value > threshold
        ]

def build():
    return P95LatencyDetector()
```

---

## 6. L0–L3 四层漏斗（核心）

漏斗：原始信号 → 抑制后 → 候选异常 → 事件 → 记录，**数量递减、可信度递增**。

```
原始信号 ──L0──► 过滤后 ──L1──► 候选异常 ──L2──► 事件 ──L3──► problem_record
  1000         800         200          50         10
```

### 6.1 DetectionContext / DomainResult（状态载体）

```python
# pipeline/context.py
@dataclass
class DetectionContext:
    trace_id: str
    tenant_id: str = "default"       # 多租户隔离（请求头 X-Tenant-Id，默认 default）
    domain: str
    domain_config: dict              # 来自 MySQL domain_config 的检测规则
    registry: PluginRegistry
    storage: RecordStore
    now: datetime

    targets: list = field(default_factory=list)     # 本轮采集的监控端点（monitor_target 行）
    signals: list = field(default_factory=list)     # 采集到的 Metric/Log 信号
    changes: list = field(default_factory=list)     # 变更信号（供 L2 关联）
    suppressed: list = field(default_factory=list)  # L0 抑制记录（审计）
    anomalies: list = field(default_factory=list)   # L1 产出

    # 动态配置（每轮从 DynamicConfigStore 载入）
    maintenance_windows: list = field(default_factory=list)
    blacklist: list = field(default_factory=list)
    fpr: dict = field(default_factory=dict)          # group_key -> 误报率

    # 跨轮状态（由 DetectionState 注入）
    previous_keys: set = field(default_factory=set)  # 上一轮 anomaly keys

@dataclass
class DomainResult:
    domain: str
    records: list              # 本域产出的 ProblemRecord
    suppressed_count: int
    anomaly_count: int
    degraded_sources: list
```

### 6.2 collect（采集，并行 + 降级）

```python
async def collect(ctx):
    tasks = []
    for t in ctx.targets:                         # targets = 本轮启用的 monitor_target
        col = ctx.registry.get("collector", collector_for(t))
        tasks.append(col.collect(ctx, t))         # 传整个 target（含 source_config + schedule）
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for t, r in zip(ctx.targets, results):
        if isinstance(r, Exception):
            ctx.degraded_sources.append(t["target_id"])   # 单源失败降级，不崩溃
        else:
            for s in r:
                if isinstance(s, ChangeSignal):
                    ctx.changes.append(s)
                else:
                    ctx.signals.append(s)
```

`collector_for(t)` 按 `signal_type + source_type` 选择 collector 插件：

| signal_type | source_type | collector 插件 |
|-------------|-------------|----------------|
| log | http / elk | `http_logs` |
| metric | prometheus / http | `http_metrics` |


**降级原则**：单源失败 → 记 `degraded_sources`，继续跑其余源；全部失败 → 空信号自然在 L3 前终止（零 LLM 调用）。

### 6.3 L0 抑制（Suppression）

```python
async def l0_suppress(ctx):
    kept, suppressed = [], []
    for s in ctx.signals:
        reason = None
        for sc in ctx.domain_config.get("suppressors", []):
            sup = ctx.registry.get("suppressor", sc["name"])
            r = await sup.check(s, ctx, sc.get("params", {}))
            if r:
                reason = r
                break
        (suppressed if reason else kept).append(s if not reason else {"signal": s, "reason": reason})
    ctx.suppressed = suppressed
    ctx.signals = kept
```

内置两个 Suppressor 插件：

| 插件 | 逻辑 | 数据源 |
|------|------|--------|
| `maintenance_window` | 信号 `timestamp` 落在维护窗口内 → 抑制 | `maintenance_window` 表（动态） |
| `blacklist` | 命中 `domain:service:signal` 黑名单 → 抑制 | `suppress_blacklist` 表（动态） |

### 6.4 L1 检测（Baseline / 规则分发）

```python
async def l1_detect(ctx):
    for dc in ctx.domain_config.get("detectors", []):
        detector = ctx.registry.get("detector", dc["plugin"])
        matched = filter_signals(ctx.signals, dc.get("signal"))
        if not matched:
            continue
        for a in await detector.detect(matched, dc.get("params", {})):
            a.method = detector.name
            a.severity = dc.get("severity", a.severity)
            ctx.anomalies.append(a)
```

`filter_signals(signals, target)`：`target` 为 `*`/空 → 全量；否则按 `MetricSignal.metric == target` 或 `LogSignal.level == target` 过滤。

内置三个 Detector 插件：

| 插件 | 信号类型 | 逻辑 | 关键参数 |
|------|---------|------|---------|
| `static_threshold` | 指标 | `value > threshold`（可配 `operator`） | `threshold`, `operator` |
| `simple_compare` | 指标 | `value > baseline * ratio`（当前值 vs 基线，基线来自 params 或 label） | `baseline`, `ratio` |
| `signature_aggregate` | 日志 | 堆栈签名聚合，`count >= min_count` 判定突增 | `min_count`, `n_frames` |

**堆栈签名**（`signature_aggregate` 内部）：

```
signature = 异常类型 + 顶部 N 帧（类名+方法名，去行号，N=3~5）
47 条相同 OOM → 1 条 log_anomaly(count=47)
```

```python
def signature(log: LogSignal, n_frames: int = 3) -> str:
    if not log.stack_trace:
        return log.message[:120]
    lines = log.stack_trace.strip().split("\n")
    exc = lines[0].split(":")[0] if lines else log.message
    frames = [ln.strip().split("(")[0] for ln in lines[1:1 + n_frames]]
    return "|".join([exc, *frames])
```

> **v1 检测方法限定**：`static_threshold` + `simple_compare` + `signature_aggregate`。`dynamic_baseline`（zscore）/ ML 基线留 V2 接入。

### 6.5 L2 关联（Semantic）

```python
def l2_correlate(ctx) -> tuple[Correlation, bool]:
    metric_anoms = [a for a in ctx.anomalies if isinstance(a, MetricAnomaly)]
    log_anoms   = [a for a in ctx.anomalies if isinstance(a, LogAnomaly)]

    related, reason = False, "single-source"
    if metric_anoms and log_anoms:
        window = ctx.domain_config["correlation"].get("metric_log_window_sec", 300)
        same_svc = any(m.service == l.service for m in metric_anoms for l in log_anoms)
        within   = _within_window(metric_anoms, log_anoms, window)
        if same_svc and within:
            related, reason = True, "metric_log_within_window"
        else:
            reason = "unrelated"

    change_related = _change_within_window(ctx.changes, ctx.anomalies,
                                          ctx.domain_config["correlation"].get("change_window_sec", 300))
    return Correlation(related=related, reason=reason), change_related
```

**域内关联 4 情况**：① 仅指标 → 独立；② 仅日志 → 独立；③ 同 service + 时间窗内 → 合并（`related=true`）；④ 两者但无关 → 各自独立。

**变更关联**：异常 ±`change_window_sec` 内有 `change_record` → `change_related=true` + `recent_change`。

**LLM 摘要（可选，不阻塞）**：L2 可调用轻量 LLM 生成 `symptom.summary`；失败重试 2 次后**用模板兜底**（从 anomaly 结构化数据拼摘要，标 `degraded`），绝不因 LLM 失败阻塞开单。

```python
def template_summary(metric_anoms, log_anoms) -> str:
    # 兜底：如 "payment-service cpu_usage 飙高至 0.92 且出现 OutOfMemoryError 堆栈"
    ...
```

### 6.6 L3 验证（Verification，最后闸门）

```python
async def l3_verify(ctx, correlation) -> Verification:
    vc = ctx.domain_config.get("verify", {})
    persistence_rounds = vc.get("persistence_rounds", 2)
    fpr_threshold = vc.get("false_positive_threshold", 0.6)

    cur_keys = anomaly_keys(ctx.anomalies)
    persistence_ok = (persistence_rounds <= 1) or bool(cur_keys & ctx.previous_keys)

    group_key = group_key_for(ctx.tenant_id, ctx.domain, ctx.anomalies)
    fpr = ctx.fpr.get(group_key, 0.0)
    fpr_ok = fpr < fpr_threshold

    resample_ok = True   # v1 简化为 True；二次采样 v2 接入

    passed = bool(ctx.anomalies) and persistence_ok and fpr_ok and resample_ok
    return Verification(passed=passed, persistence_ok=persistence_ok,
                        resample_ok=resample_ok, false_positive_rate=fpr,
                        final_severity=calibrate_severity(ctx.anomalies))
```

| 验证项 | 通过条件 |
|--------|----------|
| 持续性 | 异常 key 与上一轮有交集（或 `persistence_rounds<=1`） |
| 误报率 | `fpr_table` 命中 < 阈值（默认 0.6） |
| 二次采样 | v1 恒 True，v2 接二次采样 |
| 严重度校准 | 取最高 severity；组合信号（heap 高 + Full GC 突增）升 critical |

> **误报率反馈闭环**：下游诊断判定误报后回写 `fpr_table`，L3 读取。v1 建表 + 读取，回写由 v2 诊断接入。

### 6.7 emit（组装 + 去重落库）

```python
async def emit(ctx, correlation, verification) -> list[ProblemRecord]:
    if not verification.passed:
        return []
    records = []
    for service, anoms in group_by_service(ctx.anomalies).items():
        rec = ProblemRecord(
            record_id=new_record_id(),            # PR-YYYYMMDD-NNNN
            source="apm-alert",
            tenant_id=ctx.tenant_id,
            domain=ctx.domain, state="pending",
            service=service, detected_at=ctx.now,
            symptom={"summary": template_summary(...),
                     "severity": verification.final_severity},
            metric_anomalies=[...], log_anomalies=[...],
            correlation=correlation,
            change_related=change_related, recent_change=...,
            verification=verification, trace_id=ctx.trace_id,
        )
        # 去重（方案 A：按 record 生命周期去重）
        existing = await ctx.storage.find_open(rec.group_key)
        if existing:
            await ctx.storage.update(existing["record_id"],
                                     evidence=rec.metric_anomalies + rec.log_anomalies,
                                     reason="dedup_append")
        else:
            await ctx.storage.write(rec)
            records.append(rec)
    return records
```

**去重生命周期（方案 A）**：

| 场景 | 判定 | 动作 |
|------|------|------|
| 同 `group_key`，record 未关闭 | 命中 | `update` 追加 evidence，不新开单 |
| 同 `group_key`，record 已 closed/archived | 未命中 | 视为复发，`write` 新开单 |
| 无同 `group_key` | 未命中 | 首单，`write` 新开单 |

`group_key = tenant_id:domain:service:anomaly_type`（anomaly_type 取指标名/签名聚类键，稳定可复现；租户内唯一）。

---

## 7. 数据模型

### 7.0 全流程存储全景

整个闭环（配置 → 调度 → 采集检测 → 开单 → 处置 → 反馈）需要落库的数据实体如下。**v1 已有 7 张核心表已覆盖最小闭环**；`signal_snapshot` 等 3 张为 v2 建议补充。

全部表统一放在单一 schema `aiops_apm_runtime`（一个库承载业务配置、输出与运行时/历史数据）。

> **多租户**：以上所有表均含 `tenant_id` 列（默认 `default`），租户级唯一键如 `(tenant_id, group_key)`、`(tenant_id, target_id)`、`(tenant_id, domain)`、`(tenant_id, domain, state_key)`，实现租户数据隔离。

| # | 数据实体 | 表名 | Schema | 数据类别 | 产生环节 | 状态 |
|---|---------|------|--------|---------|---------|------|
| 1 | 监控端点 | `monitor_target` | aiops_apm_runtime | 配置（人驱动） | 自服务新增监控 | v1 已有 |
| 2 | 检测规则 | `domain_config` | aiops_apm_runtime | 配置（人驱动） | 规则配置 | v1 已有 |
| 3 | 维护窗口 | `maintenance_window` | aiops_apm_runtime | 配置·抑制 | 计划内变更 | v1 已有 |
| 4 | 黑名单 | `suppress_blacklist` | aiops_apm_runtime | 配置·抑制 | 已知噪音 | v1 已有 |
| 5 | 变更记录 | `change_record` | aiops_apm_runtime | 输入（外部） | CI/CD 写入 | v1 已有 |
| 6 | 信号历史快照 | `signal_snapshot` | aiops_apm_runtime | 中间态 | collect 采集 | v2 建议 |
| 7 | 跨轮检测状态 | `detection_state` | aiops_apm_runtime | 中间态 | L3 持续性 | v2 建议 |
| 8 | 轮次审计 | `detection_round` | aiops_apm_runtime | 审计 | 每轮执行 | v2 建议（可选） |
| 9 | 问题记录 | `problem_record` | aiops_apm_runtime | 输出 | emit 开单 | v1 已有 |
| 10 | 误报率 | `fpr_table` | aiops_apm_runtime | 反馈 | 下游误报回写 | v1 已有 |

**v2 补充表的用途**：

- `signal_snapshot`：保存采集到的原始指标/日志快照，支撑 `simple_compare` 的「真实历史基线」、`dynamic_baseline`（zscore）与 ML 趋势检测（v1 的 `baseline` 是规则里硬编码的，不依赖历史）。
- `detection_state`：持久化 L3 持续性的「上一轮 anomaly keys」（v1 在内存 `DetectionState`，进程重启即丢）。
- `detection_round`（可选）：落库每轮 `trace_id` + timeline + `degraded_sources`，把可观测性从 Prometheus/日志扩展到可查询审计。

### 7.1 Pydantic 模型（进程内契约）

```python
# models/signal.py
class MetricSignal(BaseModel):
    tenant_id: str = "default"
    service: str
    metric: str                    # cpu_usage / jvm_heap_used / error_rate ...
    value: float
    timestamp: datetime
    labels: dict = {}

class LogSignal(BaseModel):
    tenant_id: str = "default"
    service: str
    level: str                     # ERROR / WARN
    message: str
    stack_trace: str | None = None
    timestamp: datetime
    trace_id: str | None = None

class ChangeSignal(BaseModel):
    tenant_id: str = "default"
    service: str
    change_id: str                 # CHG-YYYYMMDD-NNNN
    type: str                      # deployment / ddl / config
    summary: str
    timestamp: datetime

Signal = MetricSignal | LogSignal | ChangeSignal
```

```python
# models/anomaly.py
class MetricAnomaly(BaseModel):
    service: str
    metric: str
    value: float
    baseline: float | None = None
    method: str                    # detector 插件名
    severity: str                  # warning / high / critical
    detected_at: datetime

class LogAnomaly(BaseModel):
    service: str
    level: str
    signature: str
    pattern: str
    count: int
    first_seen: datetime
    severity: str

Anomaly = MetricAnomaly | LogAnomaly
```

```python
# models/record.py
class Correlation(BaseModel):
    related: bool
    reason: str

class Verification(BaseModel):
    passed: bool
    persistence_ok: bool
    resample_ok: bool
    false_positive_rate: float
    final_severity: str

class ProblemRecord(BaseModel):
    record_id: str
    source: str                     # 固定 "apm-alert"（来源模块，非 agent id）
    tenant_id: str = "default"      # 多租户隔离
    domain: str
    state: str = "pending"         # pending/in_progress/resolved/closed/archived
    service: str
    instance: str | None = None
    detected_at: datetime
    symptom: dict                  # {summary, severity}
    metric_anomalies: list[MetricAnomaly]
    log_anomalies: list[LogAnomaly]
    correlation: Correlation
    change_related: bool = False
    recent_change: dict | None = None
    verification: Verification
    evidence: list[dict] = []
    trace_id: str | None = None
    @property
    def group_key(self) -> str:    # tenant_id:domain:service:anomaly_type
        ...
```

**写入约束**：`metric_anomalies` 与 `log_anomalies` 至少一个非空，且 `verification.passed=true`。

### 7.2 DDL（aiops_apm_runtime 库，Python 直连）

```sql
CREATE DATABASE IF NOT EXISTS aiops_apm_runtime
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE aiops_apm_runtime;

CREATE TABLE IF NOT EXISTS problem_record (
    record_id        VARCHAR(32)   NOT NULL PRIMARY KEY COMMENT 'PR-YYYYMMDD-NNNN',
    group_key        VARCHAR(255)  NOT NULL COMMENT 'tenant_id:domain:service:anomaly_type 去重键',
    source           VARCHAR(64)   NOT NULL COMMENT '记录来源模块（固定 apm-alert）',
    tenant_id        VARCHAR(64)   NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    domain           VARCHAR(32)   NOT NULL,
    state            VARCHAR(16)   NOT NULL DEFAULT 'pending',
    service          VARCHAR(64)   NOT NULL,
    instance         VARCHAR(128)  DEFAULT NULL,
    detected_at      DATETIME(3)   NOT NULL,
    symptom          JSON,
    metric_anomalies JSON,
    log_anomalies    JSON,
    correlation      JSON,
    change_related   TINYINT(1)    NOT NULL DEFAULT 0,
    recent_change    JSON,
    verification     JSON,
    evidence         JSON          COMMENT '去重时追加的证据',
    trace_id         VARCHAR(64)   DEFAULT NULL,
    created_at       DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at       DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    INDEX idx_group_key (group_key),
    INDEX idx_tenant_state (tenant_id, state),
    INDEX idx_tenant_domain_service (tenant_id, domain, service),
    INDEX idx_detected_at (detected_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS change_record (
    change_id     VARCHAR(32)  NOT NULL PRIMARY KEY,
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    service       VARCHAR(64)  NOT NULL,
    type          VARCHAR(16)  NOT NULL COMMENT 'deployment/ddl/config',
    summary       VARCHAR(500) DEFAULT NULL,
    changed_at    DATETIME(3)  NOT NULL,
    metadata      JSON,
    created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_service_time (tenant_id, service, changed_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS domain_config (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id  VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    domain     VARCHAR(32)  NOT NULL COMMENT '域 id，如 application',
    config     JSON         NOT NULL COMMENT '域检测规则(detectors/suppressors/correlation/verify)',
    enabled    TINYINT(1)   NOT NULL DEFAULT 1,
    version    INT          NOT NULL DEFAULT 1,
    updated_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_tenant_domain (tenant_id, domain)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS monitor_target (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    target_id     VARCHAR(32)  NOT NULL COMMENT '对外唯一 id，如 MT-0001',
    service       VARCHAR(64)  NOT NULL COMMENT '被监控服务，如 order-management',
    signal_type   VARCHAR(16)  NOT NULL COMMENT 'log / metric',
    source_type   VARCHAR(16)  NOT NULL COMMENT 'http / prometheus / elk',
    domain        VARCHAR(32)  NOT NULL DEFAULT 'application' COMMENT '归属域（决定应用哪套检测规则）',
    source_config JSON         NOT NULL COMMENT '采集端点配置(url/method/headers/params/field_mapping)',
    schedule      JSON         NOT NULL COMMENT '定时任务(interval_sec 或 cron)',
    enabled       TINYINT(1)   NOT NULL DEFAULT 1,
    created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_tenant_target_id (tenant_id, target_id),
    INDEX idx_tenant_service (tenant_id, service),
    INDEX idx_tenant_enabled (tenant_id, enabled)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS maintenance_window (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id  VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    service    VARCHAR(64)  NOT NULL,
    start_at   DATETIME(3)  NOT NULL,
    end_at     DATETIME(3)  NOT NULL,
    reason     VARCHAR(255) DEFAULT NULL,
    created_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_service_time (tenant_id, service, start_at, end_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS suppress_blacklist (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id  VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    domain     VARCHAR(32)  NOT NULL,
    service    VARCHAR(64)  NOT NULL,
    signal     VARCHAR(64)  NOT NULL COMMENT 'metric/log pattern',
    reason     VARCHAR(255) DEFAULT NULL,
    enabled    TINYINT(1)   NOT NULL DEFAULT 1,
    created_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_domain_service (tenant_id, domain, service)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS fpr_table (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id           VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    group_key           VARCHAR(255) NOT NULL COMMENT 'tenant_id:domain:service:anomaly_type',
    false_positive_cnt  BIGINT NOT NULL DEFAULT 0,
    total_cnt           BIGINT NOT NULL DEFAULT 0,
    fpr                 DECIMAL(5,4) NOT NULL DEFAULT 0,
    updated_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_tenant_group_key (tenant_id, group_key)
) ENGINE=InnoDB;
```

### 7.3 DDL（v2 运行时/历史表，同一 `aiops_apm_runtime` 库）

以下 3 张 v2 表与 §7.2 同属单一 `aiops_apm_runtime` 库（不再独立 schema）。信号快照量大，建议按 `snapshot_ts` 分区/定期归档；状态/审计生命周期短可独立清理。

```sql
-- 信号历史快照：采集到的原始指标/日志，支撑基线/环比/ML（量大，建议分区/归档）
CREATE TABLE IF NOT EXISTS signal_snapshot (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    snapshot_ts   DATETIME(3)  NOT NULL COMMENT '采集轮次时间',
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    target_id     VARCHAR(32)  NOT NULL COMMENT '来源监控端点',
    service       VARCHAR(64)  NOT NULL,
    domain        VARCHAR(32)  NOT NULL,
    signal_type   VARCHAR(16)  NOT NULL COMMENT 'metric / log',
    metric        VARCHAR(64)  DEFAULT NULL COMMENT 'signal_type=metric',
    value         DOUBLE       DEFAULT NULL COMMENT 'signal_type=metric',
    level         VARCHAR(16)  DEFAULT NULL COMMENT 'signal_type=log',
    message       TEXT         DEFAULT NULL COMMENT 'signal_type=log',
    signature     VARCHAR(255) DEFAULT NULL COMMENT '日志堆栈签名',
    labels        JSON         DEFAULT NULL COMMENT 'metric labels / log 附加字段',
    created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_target_time (tenant_id, target_id, snapshot_ts),
    INDEX idx_tenant_service_metric (tenant_id, service, metric, snapshot_ts),
    INDEX idx_tenant_service_level (tenant_id, service, level, snapshot_ts)
) ENGINE=InnoDB COMMENT='原始信号快照，量大，建议按 snapshot_ts 分区/定期归档';

-- 跨轮检测状态：L3 持续性的「上一轮 anomaly keys」等
CREATE TABLE IF NOT EXISTS detection_state (
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    domain        VARCHAR(32)  NOT NULL,
    state_key     VARCHAR(64)  NOT NULL COMMENT '如 previous_keys',
    state_value   JSON         NOT NULL,
    updated_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (tenant_id, domain, state_key)
) ENGINE=InnoDB;

-- 轮次审计：每轮 trace_id + 阶段统计 + timeline
CREATE TABLE IF NOT EXISTS detection_round (
    round_id          VARCHAR(64)  NOT NULL PRIMARY KEY COMMENT '即 trace_id',
    tenant_id         VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    started_at        DATETIME(3)  NOT NULL,
    finished_at       DATETIME(3)  DEFAULT NULL,
    status            VARCHAR(16)  NOT NULL DEFAULT 'running' COMMENT 'running/success/partial/failed',
    target_ids        JSON         COMMENT '本轮涉及的监控端点',
    signals_count     INT          NOT NULL DEFAULT 0,
    anomaly_count     INT          NOT NULL DEFAULT 0,
    record_count      INT          NOT NULL DEFAULT 0,
    suppressed_count  INT          NOT NULL DEFAULT 0,
    degraded_sources  JSON         COMMENT '降级的采集源',
    timeline          JSON         COMMENT '各阶段耗时/时间戳',
    created_at        DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_started_at (tenant_id, started_at)
) ENGINE=InnoDB;
```

---

## 8. 配置 Schema（手动可编辑）

配置分四类：**监控端点（`monitor_target`，自服务）**、**检测规则（`domain_config`）**、**静态 YAML（harness / seed）**、**运行时动态配置（DB，热更新）**。

> 二者职责分离：`monitor_target` 回答「监控谁、从哪采、多快采」，`domain_config` 回答「采到后怎么判、怎么抑制、怎么验证」。新增监控服务只需加一行 `monitor_target`，无需改检测规则。

### 8.1 监控端点（MySQL `monitor_target` 表，自服务）

**核心用例**：运维在界面输入 `service=order-management`、`类型=日志`、`http 端点`、`定时任务`，提交即开始对该服务的日志监控。

| 字段 | 含义 | 示例 |
|------|------|------|
| `tenant_id` | 所属租户（多租户隔离，由请求头 `X-Tenant-Id` 注入） | `default` |
| `target_id` | 端点唯一 id（租户内唯一） | `MT-0001` |
| `service` | 被监控服务 | `order-management` |
| `signal_type` | 信号类型 | `log` / `metric` |
| `source_type` | 采集源类型 | `http` / `prometheus` / `elk` |
| `domain` | 归属域（决定用哪套检测规则） | `application`（默认） |
| `source_config` | 采集端点配置（JSON） | 见下 |
| `schedule` | 定时任务（JSON） | `{"interval_sec": 60}` 或 `{"cron": "*/5 * * * *"}` |
| `enabled` | 是否启用 | `1` |

```jsonc
// monitor_target 表一行示例（service=order-management，日志监控）
{
  "tenant_id": "default",
  "target_id": "MT-0001",
  "service": "order-management",
  "signal_type": "log",
  "source_type": "http",
  "domain": "application",
  "source_config": {
    "url": "http://order-management:9200/logs/_search",
    "method": "POST",
    "headers": { "Authorization": "Bearer ${ORDER_MGMT_TOKEN}" },
    "params": { "level": "ERROR", "size": 200 },
    "field_mapping": { "level": "level", "message": "message", "stack_trace": "stack_trace", "timestamp": "@timestamp" }
  },
  "schedule": { "interval_sec": 60 },
  "enabled": true
}
```

```jsonc
// 指标监控示例（service=order-management，Prometheus 拉取）
{
  "tenant_id": "default",
  "target_id": "MT-0002",
  "service": "order-management",
  "signal_type": "metric",
  "source_type": "prometheus",
  "domain": "application",
  "source_config": {
    "url": "http://prometheus:9090/api/v1/query",
    "params": { "query": "cpu_usage{service=\"order-management\"}" },
    "field_mapping": { "metric": "metric", "value": "value[1]", "timestamp": "timestamp" }
  },
  "schedule": { "interval_sec": 60 },
  "enabled": true
}
```

- **`source_config`**：完全由 collector 插件解释。`http_logs`/`http_metrics` 识别 `url/method/headers/params/field_mapping`；`field_mapping` 把第三方响应字段映射到 `LogSignal`/`MetricSignal`。
- **自服务入口**：`POST /v1/monitors` 新增、`PUT/DELETE /v1/monitors/{target_id}` 修改/删除、`POST /v1/monitors/{target_id}/run` 立即执行（见 §10）。提交后 `scheduler` 按 `schedule` 自动调度，无需重启进程。

### 8.2 检测规则（MySQL `domain_config` 表，运行时加载）

检测规则以 **MySQL `domain_config` 表为主源**，YAML 仅作**首次初始化的 seed**（及无 DB 时的兜底）。

**存储结构**：每个「租户 + 域」一行，`config` 列存该域的检测规则 JSON（detectors/suppressors/correlation/verify），唯一键 `(tenant_id, domain)`。

```jsonc
// domain_config 表一行示例（domain=application 的 config 列）
{
  "detectors": [
    { "signal": "cpu_usage",  "plugin": "static_threshold",    "params": { "threshold": 0.9 }, "severity": "high" },
    { "signal": "error_rate", "plugin": "simple_compare",      "params": { "ratio": 1.5, "baseline": 0.02 }, "severity": "high" },
    { "signal": "ERROR",      "plugin": "signature_aggregate", "params": { "min_count": 5, "n_frames": 3 }, "severity": "warning" }
  ],
  "suppressors": [
    { "name": "maintenance_window" },
    { "name": "blacklist" }
  ],
  "correlation": { "metric_log_window_sec": 300, "change_window_sec": 300 },
  "verify": { "persistence_rounds": 2, "false_positive_threshold": 0.6 }
}
```

**加载逻辑**（`config/loader.py`）：

```python
class DomainConfigLoader:
    def __init__(self, store: DomainConfigStore, yaml_seed_path=None): ...

    async def load(self, tenant_id: str) -> list[dict]:
        rows = await self.store.load_domain_configs(tenant_id)   # 该租户 enabled=1 的域
        if rows:
            return [self._to_domain(r) for r in rows]
        # 表为空（首次启动 / 无 DB）→ 用 YAML seed 幂等写入，再返回
        seed = self._load_yaml_seed()                      # config/domains.yaml
        await self.store.seed(tenant_id, seed)             # INSERT ... ON DUPLICATE KEY UPDATE
        return seed
```

- **加载时机**：启动时加载一次；每轮（或每 N 轮 / 手动 `POST /v1/config/reload`）刷新，实现「改库即生效」。
- **手动配置**：直接改 `domain_config` 表即可（或后续提供配置管理 API 统一入口）。

> `detectors[].signal` 既可是**指标名**（`cpu_usage`）也可是**日志级别**（`ERROR`），由 `filter_signals` 按信号类型匹配。`detectors[].plugin` 引用 entry_points 注册的 detector 名；`params` 是传给该 detector 的规则参数。

### 8.3 静态 YAML

`config/domains.yaml` 仅作 seed（`domain_config` 为空时生效），结构与 `domain_config.config` 一致：

```yaml
# config/domains.yaml（seed）
domains:
  - id: application
    enabled: true
    detectors:
      - { signal: cpu_usage,   plugin: static_threshold, params: { threshold: 0.9 }, severity: high }
      - { signal: error_rate,  plugin: simple_compare,   params: { ratio: 1.5, baseline: 0.02 }, severity: high }
      - { signal: ERROR,       plugin: signature_aggregate, params: { min_count: 5, n_frames: 3 }, severity: warning }
    suppressors:
      - { name: maintenance_window }
      - { name: blacklist }
    correlation: { metric_log_window_sec: 300, change_window_sec: 300 }
    verify: { persistence_rounds: 2, false_positive_threshold: 0.6 }
```

```yaml
# config/harness.yaml（静态）
harness:
  scheduler_tick_sec: 1        # 调度器 tick 粒度（检查到期端点）
  total_timeout_sec: 50        # 单轮总超时
  degrade_on_source_failure: true
```

### 8.4 运行时动态配置（DB，热更新）

| 配置 | 存储表 | 读取层 |
|------|--------|--------|
| 监控端点 | `monitor_target` | 每轮载入（§8.1） |
| 检测规则 | `domain_config` | 每轮载入（§8.2） |
| 维护窗口 | `maintenance_window` | L0 `maintenance_window` 插件 |
| 黑名单 | `suppress_blacklist` | L0 `blacklist` 插件 |
| 误报率 | `fpr_table` | L3 验证 |

---

## 9. 存储层设计（MySQL 直连）

```python
# storage/connection.py
class ConnectionPool:
    def __init__(self, settings): ...
    async def connect(self):            # aiomysql.create_pool(...)
    async def close(self): ...
    async def execute(self, sql, args): ...
    async def fetchone(self, sql, args): ...
    async def fetchall(self, sql, args): ...

# storage/records.py —— 抽象出最小接口，MySQL 为主实现，InMemory 用于 demo/单测
class RecordStore(ABC):
    async def write(self, record: ProblemRecord) -> None: ...
    async def update(self, record_id, evidence, reason) -> None: ...
    async def find_open(self, group_key) -> dict | None: ...   # 租户内 state ∉ {closed,archived}
    async def list(self, tenant_id, state=None, limit=100) -> list[dict]: ...

class MySQLRecordStore(RecordStore): ...     # JSON 字段 json.dumps 序列化
class InMemoryRecordStore(RecordStore): ...  # demo/单测兜底
```

```python
# storage/monitor_target.py
class MonitorTargetStore(ABC):
    async def load_all_targets(self) -> list[dict]: ...        # 全租户 enabled=1（scheduler 用，行含 tenant_id）
    async def load_targets(self, tenant_id) -> list[dict]: ... # 该租户 enabled=1 的端点
    async def get(self, tenant_id, target_id) -> dict | None: ...
    async def create(self, target: dict) -> None: ...          # 自服务新增（含 tenant_id）
    async def update(self, tenant_id, target_id, target: dict) -> None: ...
    async def delete(self, tenant_id, target_id) -> None: ...

# storage/domain_config.py
class DomainConfigStore(ABC):
    async def load_domain_configs(self, tenant_id) -> list[dict]: ...   # 该租户 enabled=1
    async def seed(self, tenant_id, domains: list[dict]) -> None: ...   # 幂等写入（INSERT ... ON DUPLICATE KEY UPDATE）
    async def upsert(self, tenant_id, domain, config, enabled) -> None: ...

# storage/dynamic_config.py
class DynamicConfigStore(ABC):
    async def load_maintenance_windows(self, tenant_id, service) -> list: ...
    async def load_blacklist(self, tenant_id) -> list: ...
    async def load_fpr(self, tenant_id, group_key) -> float: ...
```

> `settings.storage_backend` 决定用 `mysql` 还是 `memory`（默认 `mysql`；`memory` 仅用于本地 demo/单测，避免无 MySQL 时无法跑通）。生产目标仍是 MySQL 直连，不引入 SQLite。

---

## 10. 服务 API 设计（FastAPI）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 存活探针 |
| GET | `/ready` | 就绪探针（检查插件已加载、DB 可连） |
| POST | `/v1/alerts/run` | 手动触发一轮检测（同步返回本轮结果，供调试/前端） |
| GET | `/v1/problems` | 查询 `problem_record`（支持 `state`/`service`/`limit` 过滤） |
| POST | `/v1/monitors` | 新增监控端点（自服务） |
| GET | `/v1/monitors` | 列出监控端点 |
| PUT | `/v1/monitors/{target_id}` | 修改监控端点 |
| DELETE | `/v1/monitors/{target_id}` | 删除监控端点 |
| POST | `/v1/monitors/{target_id}/run` | 立即对该端点执行一轮检测 |
| GET | `/v1/plugins` | 列出已加载插件（kind/name） |
| POST | `/v1/plugins/reload` | 重新发现插件（pick up 新安装的第三方包） |
| POST | `/v1/config/reload` | 重新加载检测规则（从 MySQL `domain_config`） |

> **多租户**：所有 `/v1/*` 接口通过请求头 `X-Tenant-Id` 携带租户（缺省 `default`）。配置写入（`/v1/monitors`）、规则加载、查询（`/v1/problems`）、执行（`/v1/alerts/run`、`/v1/monitors/{id}/run`）均按该头隔离；`tenant_id` 由服务端从请求头注入，不信任客户端 body 中的值。

```python
# 从请求头提取租户（FastAPI 依赖注入）
def tenant_id(request: Request) -> str:
    return request.headers.get("X-Tenant-Id", "default")
```

**`POST /v1/monitors` 请求体示例**（对应 §8.1 的 order-management 用例）：

```json
{
  "service": "order-management",
  "signal_type": "log",
  "source_type": "http",
  "domain": "application",
  "source_config": {
    "url": "http://order-management:9200/logs/_search",
    "method": "POST",
    "headers": { "Authorization": "Bearer ..." },
    "params": { "level": "ERROR", "size": 200 },
    "field_mapping": { "level": "level", "message": "message", "stack_trace": "stack_trace", "timestamp": "@timestamp" }
  },
  "schedule": { "interval_sec": 60 }
}
```

> 提交后服务端生成 `target_id`（`MT-0001`），`scheduler` 按 `schedule.interval_sec=60` 每 60s 拉取一次日志并走 L0–L3 检测。

```python
# _app.py —— 应用工厂 + lifespan
def create_app(settings) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = PluginRegistry().load()
    app.state.storage = build_storage(settings)     # MySQLRecordStore / InMemory
    app.include_router(api_router)
    return app

async def lifespan(app):
    # startup: 连 DB、启动 scheduler 后台任务
    # shutdown: 关 scheduler、关 DB 连接池
    ...
```

---

## 11. 调度器（scheduler.py）

```python
# scheduler.py —— 按监控端点独立调度
async def scheduler_loop(registry, storage, domains, harness_cfg, state):
    next_run = {}                        # (tenant_id, target_id) -> 下次运行时间戳（monotonic）
    while True:
        now = time.monotonic()
        targets = [t for t in await storage.monitor_targets.load_all_targets()
                   if now >= next_run.get((t["tenant_id"], t["target_id"]), 0)]
        for t in targets:
            next_run[(t["tenant_id"], t["target_id"])] = now + t["schedule"].get("interval_sec", 60)
        if targets:
            await run_round(registry, storage, domains, targets, state)
        await asyncio.sleep(harness_cfg["scheduler_tick_sec"])   # 默认 1s 粒度
```

```python
# poller.py —— 单轮执行入口（被 scheduler 调用）
async def run_round(registry, storage, domains, targets, state) -> RoundResult:
    trace_id = new_trace_id()
    results = await asyncio.gather(
        *[run_target(DetectionContext(tenant_id=t["tenant_id"], domain=t["domain"], targets=[t], ...)) for t in targets],
        return_exceptions=True,
    )
    state.update_previous_keys(...)      # 供下一轮 L3 持续性判断
    return RoundResult(trace_id=trace_id, ...)
```

- 每轮开始前：`MonitorTargetStore` 载入启用端点（`monitor_target`）+ `DomainConfigLoader` 载入检测规则（`domain_config`），均按 `tenant_id` 过滤，并按 `target.domain` 关联检测规则。
- 每个端点独立 `schedule`（`interval_sec` 或 `cron`），到期才触发；不同端点互不阻塞，`asyncio.gather` 并行。
- 每轮一个 `trace_id`，单轮总超时（`total_timeout_sec`）由 harness 控制。
- 调度器与 FastAPI 同进程（v1 单进程）；目标多再拆进程 + 消息队列。
- 手动 `POST /v1/monitors/{target_id}/run` 复用 `run_round(..., targets=[t])`，只跑该端点。

---

## 12. 错误处理与降级

| 错误类别 | 处理策略 |
|---------|---------|
| 采集源失败 | `return_exceptions=True` + `degraded_sources` 标记，继续跑其余源 |
| 插件加载失败 | 单个插件异常跳过并告警，不拖垮启动 |
| LLM 摘要失败 | 重试 2 次 → 模板兜底（`degraded`），不阻塞开单 |
| 单轮超时 | `total_timeout_sec` 限制单轮总时长 |
| 单轮异常 | 记日志 + Prometheus 指标，不整体崩溃 |

**可观测性**：每节点追加 `timeline`（collect_done/suppressed/detected/correlated/verified/record_created）；Prometheus 指标（success_rate/records_created/degraded_sources/false_positive_rate）；结构化日志带 `trace_id + domain + signal + method`。

---

## 13. 验证用例（TDD 来源）

| # | 场景 | 拦截层 | 关键断言 |
|---|------|--------|---------|
| 1 | CPU 飙高（`cpu_usage=0.91` 两轮） | 通过 | `metric_anomalies=[cpu]`，severity=high |
| 2 | 内存泄漏（heap 递增 + Full GC） | 通过 | 组合信号升 critical |
| 3 | 代码 bug（47 条 OOM 堆栈，无指标） | 通过 | 纯日志开单，`count=47` |
| 4 | 连接池耗尽（指标+日志同源） | 通过 | `correlation.related=true` 归并一条 |
| 5 | 错误率突增 + 部署变更 | 通过 | `change_related=true` |
| 6 | 瞬时抖动（单轮偏离） | L3 持续性 | 不生成 |
| 7 | 维护窗口 | L0 抑制 | 不生成 |
| 8 | 误报率过高（fpr=0.7） | L3 误报率 | 不生成 |
| 9 | 无信号 | 提前终止 | 零 LLM 调用 |
| 10 | 日志源超时 | 降级 | record 带 `degraded`，不崩溃 |
| 11 | 单条 info 弱信号 | — | 不升级为事件 |

---

## 14. 待补充 / 开放问题

> 以下点留待后续补充，设计文档已预留接口边界。

| # | 待补充点 | 说明 |
|---|---------|------|
| 1 | **第三方 API 具体格式** | `http_metrics` / `http_logs` collector 的响应 JSON 契约、鉴权（Bearer/API Key）、分页策略 |
| 2 | **采集源清单** | 具体对接哪些第三方（Prometheus/ELK/自研 APM），指标与日志的字段映射 |
| 3 | **动态基线与 ML** | `dynamic_baseline`（zscore）、ML 趋势检测，V2 接入（需历史快照表数据量评估） |
| 4 | **LLM 摘要接入** | 用哪个模型（Haiku 级）、prompt 模板、schema 校验 |
| 5 | **变更流写入方** | `change_record` 由 CI/CD 管道/配置中心写入，v1 用 seed 脚本 + mock 假数据打通 |
| 6 | **record_id 生成策略** | `PR-YYYYMMDD-NNNN` 的 NNNN 序列：DB 序列 / Redis 自增 / 随机，需定 |
| 7 | **二次采样** | L3 的 `resample_ok` v1 恒 True，v2 接真实二次采样 |
| 8 | **水平扩展** | 服务上百后的 shard 多 scheduler 方案 |
| 9 | **前端展示** | problem_record 只读查询 API 是否需要 Java 侧提供 |
| 10 | **域配置管理入口** | 直接改 `domain_config` 表 vs 提供配置管理 API / 前端，需定 |
| 11 | **定时任务 cron 支持** | v1 先支持 `interval_sec`；`cron` 表达式解析、时区、错过补偿需定 |
| 12 | **端点密钥管理** | `source_config.headers` 里的 token 明文存储风险：加密落库 / 引用 secrets 管理器（Vault）需定 |
| 13 | **租户认证与配额** | `X-Tenant-Id` 的鉴权/校验（谁可访问某租户）、单租户配额（端点/轮次上限）、跨租户请求隔离需定 |
| 14 | **tenant_id 来源** | 由网关/JWT 注入 vs 客户端自报 `X-Tenant-Id`（信任边界）需定 |

