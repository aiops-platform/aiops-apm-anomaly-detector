# M1 契约层 — 历史规格归档

> 本文归档 `docs/apm-alert-implementation-plan-enhanced.md` 中 M1 小节（契约层）已实现的部分。契约在 M1 合并后**禁止再改签名，只允许加可选字段**。实现日志见 [`docs/logs/M1.md`](../logs/M1.md)。

## 目标

冻结所有进程内数据契约（Signal/Anomaly/ProblemRecord/fingerprint/插件 ABC/配置模型），后续 M2–M7 的依赖源。纯类型层，无副作用。

## 数据流定位

```
M3 采集器 ──产出──> Signal(信号) ──L0/L1──> Anomaly(异常) ──L2/L3──> ProblemRecord(问题单) ──M2──> 落库 problem_record 表
                                                    │
                          fingerprint(group_key) 决定「去重/持续性」
```

## 模型契约（已冻结）

### `models/signal.py`

```python
class MetricSignal(BaseModel):
    kind: Literal["metric"] = "metric"
    tenant_id: str = "default"
    service: str
    metric: str
    value: float
    timestamp: datetime
    labels: dict[str, str] = Field(default_factory=dict)

class LogSignal(BaseModel):
    kind: Literal["log"] = "log"
    tenant_id: str = "default"
    service: str
    level: str
    message: str
    stack_trace: str | None = None
    timestamp: datetime
    trace_id: str | None = None

class ChangeSignal(BaseModel):
    kind: Literal["change"] = "change"
    tenant_id: str = "default"
    service: str
    change_id: str
    type: str  # deployment / ddl / config
    summary: str
    timestamp: datetime

Signal = MetricSignal | LogSignal | ChangeSignal
```

### `models/anomaly.py`

```python
class MetricAnomaly(BaseModel):
    kind: Literal["metric"] = "metric"
    tenant_id: str = "default"
    service: str
    metric: str
    value: float
    baseline: float | None = None
    method: str               # detector 插件名
    severity: str             # warning / high / critical
    detected_at: datetime
    labels: dict = Field(default_factory=dict)

    def anomaly_key(self) -> str:  # 转发 fingerprint.anomaly_key

class LogAnomaly(BaseModel):
    kind: Literal["log"] = "log"
    tenant_id: str = "default"
    service: str
    level: str
    signature: str
    pattern: str
    count: int
    first_seen: datetime
    severity: str
    detected_at: datetime | None = None

    def anomaly_key(self) -> str:  # 转发 fingerprint.anomaly_key

Anomaly = MetricAnomaly | LogAnomaly
```

### `models/record.py`

- `Correlation`：`related: bool`、`reason: str`
- `Verification`：`passed: bool`、`persistence_ok: bool`、`resample_ok: bool = True`、`false_positive_rate: float = 0.0`、`final_severity: str`
- `ProblemRecord`：`record_id` / `source="apm-alert"` / `tenant_id="default"` / `domain` / `state="pending"` / `service` / `instance=None` / `severity="warning"` / `detected_at` / `first_seen_at=None` / `last_seen_at=None` / `occurrence_count=1` / `resolved_at=None` / `resolve_reason=None` / `symptom: dict` / `metric_anomalies` / `log_anomalies` / `correlation` / `change_related=False` / `recent_change=None` / `verification` / `evidence: list[dict]=[]` / `trace_id=None`
  - `@property group_key` → `fingerprint.group_key(tenant_id, domain, service, metric_anomalies + log_anomalies)`

### `models/config.py`

- `DetectorSpec`：`signal: str | dict`、`plugin: str`、`params: dict = {}`、`severity: str = "warning"`
- `SuppressorSpec`：`name: str`、`params: dict = {}`
- `CorrelationSpec`：`metric_log_window_sec: int = 300`、`change_window_sec: int = 300`
- `VerifySpec`：`persistence_rounds: int = 2`、`false_positive_threshold: float = 0.6`、`min_samples: int = 20`
- `DomainConfig`：`detectors: list[DetectorSpec]`、`suppressors: list[SuppressorSpec] = []`、`correlation`、`verify`

### `models/fingerprint.py`（唯一真源）

```python
def anomaly_key(a: MetricAnomaly | LogAnomaly) -> str:
    if isinstance(a, MetricAnomaly):
        raw = f"metric|{a.tenant_id}|{a.service}|{a.metric}|{sorted(a.labels.items())}"
    else:
        raw = f"log|{a.tenant_id}|{a.service}|{a.signature}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def group_key(tenant_id: str, domain: str, service: str, anomalies: list) -> str:
    keys = sorted(anomaly_key(a) for a in anomalies)
    raw = f"{tenant_id}|{domain}|{service}|{'|'.join(keys)}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"{tenant_id}:{domain}:{service}:{h}"

def is_same_group(key_a: str, key_b: str) -> bool:
    return key_a == key_b
```

### `plugins/base.py`

```python
class Plugin(ABC):
    name: str = ""

class Collector(Plugin):
    @abstractmethod
    async def collect(self, ctx, target: dict) -> list: ...

class Detector(Plugin):
    @abstractmethod
    async def detect(self, signals: list, params: dict) -> list: ...

class Suppressor(Plugin):
    @abstractmethod
    async def check(self, signal, ctx, params: dict) -> str | None: ...

    async def batch_check(self, signals: list, ctx, params: dict) -> list[tuple]:
        return [(s, await self.check(s, ctx, params)) for s in signals]

def build(*, http=None, pool=None, settings=None) -> Plugin:
    raise NotImplementedError
```

> `ctx` 类型为 `DetectionContext`（M5 pipeline 定义）；M1 实现用 `Any` 冻结接口形状，M5 后仍保持方法名/返回类型不变。

## Use Case（M1 完成标准）

| UC | 断言 |
|----|------|
| UC-1.1 | `TypeAdapter(Signal).validate_json(model_dump_json())` 后 `isinstance(..., MetricSignal)` 且 `kind=="metric"`；Log/Change 同理 |
| UC-1.2 | `anomaly_key(a1)==anomaly_key(a2)`（相同 service/metric/labels）；不同 labels → 不同 key；LogAnomaly 按 signature，相同 signature → 相同 key |
| UC-1.3 | `group_key(t,d,s,[a1,a2,a3]) == group_key(t,d,s,[a3,a1,a2])`（排序无关） |
| UC-1.4 | 合法 Detector 子类可实例化并调用；缺 `detect()` 的子类实例化抛 `TypeError` |
