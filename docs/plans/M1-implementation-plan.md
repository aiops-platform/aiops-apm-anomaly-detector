# M1 契约层（Pydantic 模型 + fingerprint 真源）— 实现计划

> 状态：**已实现**（2026-08-26）。实现日志见 [`docs/logs/M1.md`](../logs/M1.md)，历史规格归档见 [`docs/archive/M1-contracts.md`](../archive/M1-contracts.md)。

## Context（为什么做）

M0 工程基座已完成。M1 是**纯类型层**：冻结所有进程内数据契约（Signal/Anomaly/ProblemRecord/fingerprint/插件 ABC/配置模型），**这是后续所有阶段（M2–M7）的依赖源，必须最先定**。契约在 M1 合并后**禁止再改签名，只允许加可选字段**。产出无副作用，单测覆盖所有 model 序列化/判别/指纹稳定性。

- **完成标准**：`fingerprint.anomaly_key(a)` 对相同输入恒等；`group_key` 排序无关；`Signal` 反序列化能正确区分 metric/log/change
- **无前端页面**（前端技术栈未定，延续 M0 决策不做前端；不生成 TS 类型）

## 改动点：位置与用途

M1 不产生业务功能，它定义的是后续所有阶段共用的**数据契约（类型定义）**——相当于系统的「数据字典」和「接口规格」。

新增位置（与 M0 骨架平级新增，不碰 M0 的 settings/exceptions/router）：

```
src/aiops_apm/
├── models/     ← 数据模型（M1 新增）
│   ├── signal.py       # 信号（进入系统的原始数据）
│   ├── anomaly.py      # 异常（检测出的问题）
│   ├── record.py       # 记录（最终落库的问题单）
│   ├── config.py       # 配置模型（检测规则）
│   └── fingerprint.py  # 指纹函数（去重/持续性）
└── plugins/
    └── base.py         # 插件抽象基类（采集器/检测器/抑制器接口）
```

| 文件 | 在流程中的位置 | 干什么 | 谁用 |
|------|--------------|--------|------|
| **signal.py** | 管道最前端 `collect` 的产物 | 定义三类原始信号：指标（`MetricSignal` 如 cpu_usage=0.91）、日志（`LogSignal`）、变更（`ChangeSignal`）。采集器采回来的数据就是它 | M3 采集器产出；L0/L1 消费 |
| **anomaly.py** | `L1 检测` 的产物 | 定义检测出的异常：指标异常 / 日志异常。L1 的检测器把 signal 变成 anomaly | M4 检测器产出；L2/L3 消费 |
| **record.py** | 管道末端 `emit` 的产物 | 定义最终问题单 `ProblemRecord`（含关联/验证信息），就是落库到 `problem_record` 表的那条记录 | M5 emit 产出；M2 落库；下游诊断/修复 |
| **config.py** | 配置层 | 定义检测规则的模型（`DomainConfig`：配了哪些 detector、什么阈值、L3 持续性轮数等），用于解析 `domain_config` 表里的 JSON 配置 | M6 写入校验；M5 加载规则 |
| **fingerprint.py** | 横切（去重 + L3） | `anomaly_key` 给单个异常生成稳定指纹；`group_key` 给一组异常生成「排序无关」的去重键。**同一问题不会重复开单、L3 判断是否持续**都靠它 | M5 L3 持续性；M5 emit 去重；M2 去重落库 |
| **plugins/base.py** | 插件体系接口 | 定义三个抽象基类：`Collector`（采集）、`Detector`（检测）、`Suppressor`（抑制）+ `build()` 工厂。**所有第三方/内置插件都必须实现这些接口** | M3/M4 实现插件；M4 registry 发现插件 |

数据流串起来：

```
M3 采集器 ──产出──> Signal(信号) ──L0/L1──> Anomaly(异常)
                                                    │
M4 检测器实现 plugins/base 接口 ◄──契约──┘          │
                                                    ▼
M5 emit ──产出──> ProblemRecord(问题单) ──M2──> 落库 problem_record 表
                                                    │
                          fingerprint(group_key) 决定「去重/持续性」
```

- **signal / anomaly / record** 是管道三段的「接力棒」类型
- **fingerprint** 是横切的「去重真源」
- **plugins/base** 是「插件怎么写的规矩」
- **config** 是「检测规则长什么样」

## 范围

### 交付（4 个 Use Case）
| UC | 名称 | 断言 |
|----|------|------|
| UC-1.1 | Signal 序列化与判别器 | `TypeAdapter(Signal).validate_json(model_dump_json())` 后 `isinstance(..., MetricSignal)` 且 `kind=="metric"`；Log/Change 同理 |
| UC-1.2 | Anomaly 指纹稳定性 | `anomaly_key(a1)==anomaly_key(a2)`（相同 service/metric/labels）；不同 labels → 不同 key；LogAnomaly 按 signature，相同 signature → 相同 key |
| UC-1.3 | Group Key 排序无关性 | `group_key(t,d,s,[a1,a2,a3]) == group_key(t,d,s,[a3,a1,a2])` |
| UC-1.4 | 插件契约校验 | 合法 Detector 子类可实例化并调用；缺 `detect()` 的非法子类实例化抛 `TypeError` |

### 不做（明确排除）
- storage / pipeline / registry / 内置插件（M2/M3/M4/M5）
- 前端与 TS 类型生成
- 修改 settings/exceptions/_app/router（纯新增模块）

## 文件清单（新增）

```
src/aiops_apm/models/__init__.py
src/aiops_apm/models/signal.py       # MetricSignal / LogSignal / ChangeSignal / Signal 联合
src/aiops_apm/models/anomaly.py      # MetricAnomaly / LogAnomaly / Anomaly 联合
src/aiops_apm/models/record.py       # Correlation / Verification / ProblemRecord
src/aiops_apm/models/config.py       # DetectorSpec / SuppressorSpec / CorrelationSpec / VerifySpec / DomainConfig
src/aiops_apm/models/fingerprint.py  # anomaly_key / group_key / is_same_group（唯一真源）
src/aiops_apm/plugins/__init__.py
src/aiops_apm/plugins/base.py        # Plugin / Collector / Detector / Suppressor + build()
tests/test_models.py                 # UC-1.1
tests/test_fingerprint.py            # UC-1.2 + UC-1.3
tests/test_plugins_base.py           # UC-1.4
```

## 关键实现细节（契约按文档冻结）

### `models/signal.py`
- `MetricSignal`：`kind: Literal["metric"]="metric"`、`tenant_id="default"`、`service`、`metric`、`value: float`、`timestamp: datetime`、`labels: dict[str,str]=default_factory(dict)`
- `LogSignal`：`kind: Literal["log"]="log"`、`service`、`level`、`message`、`stack_trace: str|None=None`、`timestamp`、`trace_id: str|None=None`
- `ChangeSignal`：`kind: Literal["change"]="change"`、`service`、`change_id`、`type`（deployment/ddl/config）、`summary`、`timestamp`
- `Signal = MetricSignal | LogSignal | ChangeSignal`，pydantic v2 依据 `kind` 的 Literal 默认值自动判别联合

### `models/anomaly.py`
- `MetricAnomaly`：`kind="metric"`、`service`、`metric`、`value`、`baseline: float|None=None`、`method`（detector 插件名）、`severity`、`detected_at`、`labels`；`anomaly_key()` 转发 fingerprint
- `LogAnomaly`：`kind="log"`、`service`、`level`、`signature`、`pattern`、`count`、`first_seen`、`severity`、`detected_at: datetime|None=None`；`anomaly_key()` 转发 fingerprint
- `Anomaly = MetricAnomaly | LogAnomaly`

### `models/record.py`
- `Correlation`：`related: bool`、`reason: str`
- `Verification`：`passed`、`persistence_ok`、`resample_ok=True`、`false_positive_rate=0.0`、`final_severity`
- `ProblemRecord`：`record_id/source="apm-alert"/tenant_id="default"/domain/state="pending"/service/instance=None/severity="warning"/detected_at/first_seen_at=None/last_seen_at=None/occurrence_count=1/resolved_at=None/resolve_reason=None/symptom: dict/metric_anomalies/log_anomalies/correlation/change_related=False/recent_change=None/verification/evidence=default_factory(list)/trace_id=None`；`@property group_key` 转发 `fingerprint.group_key`

### `models/config.py`（契约冻结，M6 才用于写入校验）
- `DetectorSpec`：`signal: str|dict`、`plugin`、`params`、`severity="warning"`
- `SuppressorSpec`：`name`、`params`
- `CorrelationSpec`：`metric_log_window_sec=300`、`change_window_sec=300`
- `VerifySpec`：`persistence_rounds=2`、`false_positive_threshold=0.6`、`min_samples=20`
- `DomainConfig`：`detectors`、`suppressors=[]`、`correlation`、`verify`

### `models/fingerprint.py`（唯一真源）
```python
def anomaly_key(a: MetricAnomaly | LogAnomaly) -> str:
    if isinstance(a, MetricAnomaly):
        raw = f"metric|{a.tenant_id}|{a.service}|{a.metric}|{sorted(a.labels.items())}"
    else:
        raw = f"log|{a.tenant_id}|{a.service}|{a.signature}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def group_key(tenant_id, domain, service, anomalies) -> str:
    keys = sorted(anomaly_key(a) for a in anomalies)
    raw = f"{tenant_id}|{domain}|{service}|{'|'.join(keys)}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"{tenant_id}:{domain}:{service}:{h}"

def is_same_group(key_a: str, key_b: str) -> bool:
    return key_a == key_b
```

### `plugins/base.py`
- `Plugin(ABC)`：`name: str = ""`（契约冻结的抽象标记基类）
- `Collector.collect(ctx, target) -> list`、`Detector.detect(signals, params) -> list`、`Suppressor.check(signal, ctx, params) -> str|None` + 默认 `batch_check`
- `build(*, http, pool, settings) -> Plugin`：M1 占位 `raise NotImplementedError`
- 说明：文档签名 `ctx: "DetectionContext"`（DetectionContext 属 M5 pipeline 才定义）。M1 先用 `Any` 避免 mypy 未定义类型，M5 实现后仍保持方法名/返回类型不变（契约冻结的是接口形状）

## 测试（TDD，先写测试再实现）

- `tests/test_models.py`（UC-1.1）：三个 Signal 各做 `model_dump_json()` → `TypeAdapter(Signal).validate_json()` → 断言 `isinstance` 与 `kind`
- `tests/test_fingerprint.py`：
  - UC-1.2：相同 MetricAnomaly → `anomaly_key` 相等；改 labels/service → 不等；value/severity 变化不改变 key；相同 signature 的两个 LogAnomaly → 相等
  - UC-1.3：`group_key` 排序无关；格式 `tenant_id:domain:service:<hash[:12]>`；`is_same_group`；`ProblemRecord.group_key` property 转发
- `tests/test_plugins_base.py`（UC-1.4）：合法 Detector 子类实例化+调用；缺 `detect()` 的子类实例化 `pytest.raises(TypeError)`；`batch_check` 默认实现；`build()` 抛 `NotImplementedError`

## 验证（完成标准）

1. `make lint` — ruff + mypy 通过（含新 models/plugins 与测试文件）
2. `make test` — 新增 3 个测试文件全过，原 8 个用例不回归
3. 快速手动验证（可选）：python 里 `TypeAdapter(Signal).validate_json(...)` 三个 kind 正确判别

## 文档同步（CLAUDE.md 流程，同 M0）

1. 存实现计划：`docs/plans/M1-implementation-plan.md`
2. 写 `docs/logs/M1.md`：改动点、文件清单、完成状态、遗留问题
3. 归档：`docs/archive/M1-contracts.md`；实现计划 M1 小节标记「已实现」指向归档
4. 更新 `README.md`：进度表 M1 → 已完成，已实现模块补充 models/plugins
5. 更新 `CLAUDE.md`：当前里程碑状态 → M1 已完成，M2 进行中
