# APM 告警模块 — 实现计划（增强版）

> 在 `apm-alert-implementation-plan.md` 的 9 阶段计划基础上，为每个阶段补充：**前端菜单与页面**、**后端实现骨架**（关键类/函数签名）、**Use Case 清单与流程步骤**。
>
> Part A（设计校验）不变，请参阅原文档。本文为 Part B 的增强版。

---

## 阶段总览

| 阶段 | 前端菜单 | 后端核心模块 | 关键 Use Case 数 |
|------|---------|-------------|-----------------|
| M0 工程基座 | 系统状态 | settings / _app / exceptions | 3 |
| M1 契约层 | 无（纯后端） | models / fingerprint / plugins.base | 4 |
| M2 持久化 | 数据迁移状态 | storage / migrations / config.loader | 6 |
| M3 采集层 | 监控端点管理 / 采集测试 | collectors / _gateway / _http_client | 8 |
| M4 插件化 | 插件管理 / 规则配置 | plugins.registry / detectors / suppressors | 8 |
| M5 漏斗核心 | 问题列表 / 问题详情 / 检测状态 | pipeline / L0-L3 / emit / runner | 11 |
| M6 调度集成 | 调度状态 / 维护窗口 / 黑名单 / 手动触发 | scheduler / poller / reconcile / auth / router | 11 |
| M7 可观测交付 | 监控仪表盘 / 审计 / 配置版本 / 压测 | metrics / audit / security / docker-compose | 6 |

---

## 0. 先看这里：全流程框架 × 阶段映射

> **M0–M7 是「实现批次」（依赖顺序），不是业务流程顺序。** 先把你的系统在运行时拆成四个部分，再把每个 M「钉」到对应位置上，就不会迷路了。

### 整体框架（四块）

```
① 配置面（写路径）  —— 运维/用户在页面上配东西 → 存库 → 运行面读取
② 运行面（读路径）  —— 一轮检测主链：
    调度器触发 → 数据采集(指标/日志/trace) → L0 预过滤 → L1 规则检测
    → L2 同源关联 → L3 持续性 → 去重合并·开单 → 富化摘要+通知 emit
③ 展示与闭环       —— 问题列表/详情 → 状态流转·手动处理 → 恢复检测·自动关单
④ 横切层           —— 基座 / 契约 / 存储 / 可观测（不属于某一步，所有步骤共用）
```

### 流程位置 × 实现阶段对照表

| 你整个流程中的位置 | 实现阶段 | 它到底在实现什么 |
|---|---|---|
| ④ 横切：进程能起、配置能加载、日志/trace/租户上下文 | **M0 工程基座** | 地基——所有环节运行的环境，不对应流程任何一步 |
| ④ 横切：各环节之间传递的数据结构 | **M1 契约层** | 每一步的输入/输出：DetectionContext / Anomaly / fingerprint |
| ① 端点配置菜单 + ② 的「数据采集」环节 | **M3 采集层** | 流程最上游的数据来源（含水位线、出网网关） |
| ① 规则配置菜单 + ② 的「L1 规则检测」可插拔部分 | **M4 插件化** | 判定逻辑的"供给"侧（detectors/suppressors 插件注册） |
| ② 的 L0→L3→去重→开单→emit（主判定链）+ ① 通知渠道 | **M5 漏斗核心** | ★流程主干、最高风险；你 §13 用例 1–11 大部分在这里验证 |
| ② 的「调度器触发」+ ① 维护窗口/黑名单 + ③ 全部 + 所有 API | **M6 调度集成** | 谁来触发整条链 + 人工交互入口 + 恢复自动关单 |
| ④ 横切：issues/evidence/fpr/配置 落库 | **M2 持久化** | 结果侧地基；提前到 M5 之前是因为开单就要写库 |
| ④ 横切：指标/审计/压测/交付 | **M7 可观测交付** | 系统跑起来之后"怎么知道它正常" + 最终交付 |

### 为什么实现顺序是 M0→M7

一句话：**先地基和契约（M0/M1）→ 再供给（M3 数据、M4 规则，可并行）→ 再主干（M5）→ 再触发与闭环（M6）→ 最后观测交付（M7）**。M2 提前是因为 M5 一开单就需要落库。

### 竖切视角（单人开发推荐）

M1 冻结后，不等 M3/M4 全部完成：用 **mock 采集（M3 最小版）+ static_threshold（M4 最小版）+ 内存存储（M2 最小版）直通 M5 的 L0/L1/L3/emit**，先跑通 §13 用例 1/3/6/9，再回头补 L2、水位线、鉴权、调度，最后横向铺开。

---

## M0 — 工程基座（已实现）

> **状态：已完成**，实现日志见 [`docs/logs/M0.md`](../logs/M0.md)，历史规格归档见 [`docs/archive/M0-skeleton.md`](../archive/M0-skeleton.md)。实现计划见 [`docs/plans/M0-implementation-plan.md`](../plans/M0-implementation-plan.md)。

---

## M1 — 契约层（Pydantic 模型 + fingerprint 真源）（已实现）

> **状态：已完成**，实现日志见 [`docs/logs/M1.md`](../logs/M1.md)，历史规格归档见 [`docs/archive/M1-contracts.md`](../archive/M1-contracts.md)。实现计划见 [`docs/plans/M1-implementation-plan.md`](../plans/M1-implementation-plan.md)。

### 基础信息

- **目标**：冻结所有进程内数据契约；**这是后续所有阶段的依赖源，必须最先定**
- **依赖**：M0
- **冻结点**：M1 合并后**禁止**再改契约签名，只允许加可选字段
- **产出**：纯类型层，无副作用；单测覆盖所有 model 序列化/判别/指纹稳定性
- **完成标准**：`fingerprint.anomaly_key(a)` 对相同输入恒等；`group_key` 排序无关；`Signal` 反序列化能正确区分 metric/log/change

### 前端菜单与页面

> M1 为纯后端契约层，**无前端页面**。前端框架的 TypeScript 类型可从 Pydantic 模型自动生成（OpenAPI schema → TS interface），为后续阶段的前端开发提供类型基础。

前端可在此阶段生成 `types/api.ts`：

```typescript
// 从 OpenAPI schema 自动生成
export interface MetricSignal {
  kind: "metric";
  tenant_id: string;
  service: string;
  metric: string;
  value: number;
  timestamp: string;  // ISO 8601 UTC
  labels: Record<string, string>;
}

export interface LogSignal {
  kind: "log";
  tenant_id: string;
  service: string;
  level: string;
  message: string;
  stack_trace: string | null;
  timestamp: string;
  trace_id: string | null;
}

export type Signal = MetricSignal | LogSignal | ChangeSignal;
```

### 后端实现骨架

```python
# models/signal.py
from pydantic import BaseModel, Field
from typing import Literal, Any

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

# models/anomaly.py
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

    def anomaly_key(self) -> str:
        from .fingerprint import anomaly_key
        return anomaly_key(self)

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

    def anomaly_key(self) -> str:
        from .fingerprint import anomaly_key
        return anomaly_key(self)

Anomaly = MetricAnomaly | LogAnomaly

# fingerprint.py —— 唯一真源
import hashlib
from models.anomaly import MetricAnomaly, LogAnomaly

def anomaly_key(a: MetricAnomaly | LogAnomaly) -> str:
    """对单个 anomaly 生成稳定指纹。"""
    if isinstance(a, MetricAnomaly):
        raw = f"metric|{a.tenant_id}|{a.service}|{a.metric}|{sorted(a.labels.items())}"
    else:
        raw = f"log|{a.tenant_id}|{a.service}|{a.signature}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def group_key(
    tenant_id: str, domain: str, service: str, anomalies: list
) -> str:
    """对一组 anomaly 生成 group_key（排序无关）。"""
    keys = sorted(anomaly_key(a) for a in anomalies)
    # anomaly_type = 同一 service 下所有 anomaly key 排序后 hash 的前缀
    raw = f"{tenant_id}|{domain}|{service}|{'|'.join(keys)}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"{tenant_id}:{domain}:{service}:{h}"

def is_same_group(key_a: str, key_b: str) -> bool:
    return key_a == key_b

# models/record.py
class Correlation(BaseModel):
    related: bool
    reason: str

class Verification(BaseModel):
    passed: bool
    persistence_ok: bool
    resample_ok: bool = True
    false_positive_rate: float = 0.0
    final_severity: str

class ProblemRecord(BaseModel):
    record_id: str
    source: str = "apm-alert"
    tenant_id: str = "default"
    domain: str
    state: str = "pending"  # pending / in_progress / resolved / closed / archived
    service: str
    instance: str | None = None
    severity: str = "warning"  # 提升为独立字段（P1#19）
    detected_at: datetime
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    occurrence_count: int = 1
    resolved_at: datetime | None = None
    resolve_reason: str | None = None
    symptom: dict
    metric_anomalies: list[MetricAnomaly]
    log_anomalies: list[LogAnomaly]
    correlation: Correlation
    change_related: bool = False
    recent_change: dict | None = None
    verification: Verification
    evidence: list[dict] = Field(default_factory=list)
    trace_id: str | None = None

    @property
    def group_key(self) -> str:
        from .fingerprint import group_key
        all_anoms = self.metric_anomalies + self.log_anomalies
        return group_key(self.tenant_id, self.domain, self.service, all_anoms)

# models/config.py —— 用于 M6 写入校验
class DetectorSpec(BaseModel):
    signal: str | dict         # 结构化 matcher 或信号名
    plugin: str
    params: dict = Field(default_factory=dict)
    severity: str = "warning"

class SuppressorSpec(BaseModel):
    name: str
    params: dict = Field(default_factory=dict)

class CorrelationSpec(BaseModel):
    metric_log_window_sec: int = 300
    change_window_sec: int = 300

class VerifySpec(BaseModel):
    persistence_rounds: int = 2
    false_positive_threshold: float = 0.6
    min_samples: int = 20

class DomainConfig(BaseModel):
    detectors: list[DetectorSpec]
    suppressors: list[SuppressorSpec] = Field(default_factory=list)
    correlation: CorrelationSpec = Field(default_factory=CorrelationSpec)
    verify: VerifySpec = Field(default_factory=VerifySpec)

# plugins/base.py —— 插件契约
from abc import ABC, abstractmethod

class Plugin(ABC):
    name: str = ""

class Collector(Plugin):
    @abstractmethod
    async def collect(self, ctx: "DetectionContext", target: dict) -> list:
        ...

class Detector(Plugin):
    @abstractmethod
    async def detect(self, signals: list, params: dict) -> list:
        ...

class Suppressor(Plugin):
    @abstractmethod
    async def check(self, signal, ctx, params: dict) -> str | None:
        ...

    async def batch_check(self, signals: list, ctx, params: dict) -> list[tuple]:
        """批量抑制检查，默认逐条调用 check（子类可优化）。"""
        return [(s, await self.check(s, ctx, params)) for s in signals]

# build() 工厂 —— 支持依赖注入
def build(*, http=None, pool=None, settings=None) -> Plugin:
    raise NotImplementedError
```

### Use Case 清单与流程

#### UC-1.1 Signal 序列化与判别器

```
前置: M0 已完成
流程:
  1. 构造 MetricSignal(kind="metric", service="svc", metric="cpu_usage", value=0.91, ...)
  2. model_dump_json() 序列化
  3. TypeAdapter(Signal).validate_json() 反序列化
  4. 检查 kind 判别器是否正确区分为 MetricSignal
断言: 反序列化后 isinstance(result, MetricSignal) 且 result.kind == "metric"
      LogSignal / ChangeSignal 同理
```

#### UC-1.2 Anomaly 指纹稳定性

```
前置: M1 fingerprint.py 已实现
流程:
  1. 构造两个 MetricAnomaly（相同 service/metric/labels）
  2. 分别调用 anomaly_key()
  3. 比较两次结果
断言: anomaly_key(a1) == anomaly_key(a2)（相同输入恒等）
      不同 labels → 不同 key
      LogAnomaly 按 signature 去重，相同 signature → 相同 key
```

#### UC-1.3 Group Key 排序无关性

```
前置: M1 fingerprint.py 已实现
流程:
  1. 构造 anomalies = [a1, a2, a3]
  2. group_key(tenant, domain, service, anomalies)
  3. 打乱顺序 → [a3, a1, a2]
  4. 再次调用 group_key()
断言: 两次结果完全相同（排序无关）
```

#### UC-1.4 插件契约校验

```
前置: M1 plugins/base.py 已实现
流程:
  1. 定义一个合法 Detector 子类，实现 detect()
  2. 实例化并调用 detect(signals, params)
  3. 定义一个非法子类（缺少 detect 方法）
  4. 尝试实例化
断言: 合法子类可实例化并调用；非法子类抛 TypeError
```

---

## M2 — 持久化与迁移

> **已实现**（2026-08-26）。实现日志见 [`docs/logs/M2.md`](../logs/M2.md)，历史规格归档见 [`docs/archive/M2-persistence.md`](../archive/M2-persistence.md)。

## M3 — 采集层与出站网关（已实现）

> **已实现**（2026-08-26）。实现日志见 [`docs/logs/M3.md`](../logs/M3.md)，历史规格归档见 [`docs/archive/M3-collectors.md`](../archive/M3-collectors.md)，实现计划见 [`docs/plans/M3-implementation-plan.md`](../plans/M3-implementation-plan.md)。剩余项：`POST /v1/monitors/{target_id}/run` 立即执行与调度器并行降级随 M6；DNS 二次校验 / Vault 密钥管理随 M7。

### 基础信息

- **目标**：两个内置 collector 跑通真实第三方 API，且通过安全网关
- **依赖**：M1（Signal 契约、`build()` 注入）、M2（`signal_snapshot` / `collect_watermark` store）
- **关键修正**：P0#4 水位线、P0#6 SSRF/secret 在此落地
- **完成标准**：连续两轮采集同一稳定源，第二轮返回 0 新信号（水位线生效）；SSRF 测试用例被网关拒绝

### 前端菜单与页面

| 菜单路径 | 页面 | 组件 | 说明 |
|---------|------|------|------|
| 监控管理 > 监控端点 | MonitorListPage | `MonitorTable` 表格 + `CreateButton` + `SearchBar` | 列出所有 monitor_target（分页、按 service/signal_type 筛选）；新建按钮跳转表单 |
| 监控管理 > 新建端点 | MonitorFormPage | `MonitorForm` 表单 | 字段：service、signal_type（下拉：log/metric）、source_type（下拉：http/prometheus/elk）、domain、source_config（JSON 编辑器：url/method/headers/params/field_mapping）、schedule（interval_sec 或 cron）、enabled |
| 监控管理 > 端点详情 | MonitorDetailPage | `MonitorDetail` 面板 + `TestButton` + `RunNowButton` | 展示端点配置 + 采集水位线状态 + 最近采集信号数；「测试连通性」按钮调用 collector 测试采集；「立即执行」调用 POST /v1/monitors/{id}/run |
| 监控管理 > 采集测试 | CollectorTestPage | `TestResult` 面板 | 输入 url + headers，点击测试，展示网关校验结果（通过/拒绝原因）、field_mapping 预览、采集到的信号样本 |

**前端表单校验规则**：
- `url`：前端正则校验格式；后端网关二次校验（scheme 白名单、私网拒绝）
- `headers`：只接受 `${env:X}` / `${vault:path#key}` 引用语法，拒绝明文
- `field_mapping`：必填，根据 signal_type 动态切换必填字段（metric: metric/value/timestamp；log: level/message/timestamp）

### 后端实现骨架

```python
# collectors/_http_client.py
import httpx

class SharedHttpClient:
    """注入式共享 HTTP 客户端（连接池、超时、大小限制）。"""
    def __init__(self, settings: Settings):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.outbound_timeout_sec),
            limits=httpx.Limits(max_connections=50),
            follow_redirects=False,  # 禁止自动跳转
            max_content_length=settings.outbound_max_body_bytes,
        )

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return await self._client.request(method, url, **kwargs)

# collectors/_gateway.py
import ipaddress
import re
from urllib.parse import urlparse

class OutboundGateway:
    """出站安全网关。"""

    ALLOWED_SCHEMES = {"http", "https"}
    BLOCKED_NETWORKS = [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("169.254.0.0/16"),  # 云元数据
        ipaddress.ip_network("::1/128"),
    ]

    SECRET_REF_PATTERN = re.compile(r"^\$\{(env|vault):.+\}$")
    PLAINTEXT_CRED_PATTERN = re.compile(
        r"(Bearer\s+[A-Za-z0-9\-_\.]+|AKIA[A-Z0-9]{16}|"
        r"ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,})",
        re.IGNORECASE,
    )

    @classmethod
    def validate_url(cls, url: str) -> str:
        """校验 URL 安全性，返回通过则原样返回。"""
        parsed = urlparse(url)
        if parsed.scheme not in cls.ALLOWED_SCHEMES:
            raise AppException(ErrorCode.VALIDATION, f"scheme not allowed: {parsed.scheme}")
        # 解析主机 IP
        try:
            ip = ipaddress.ip_address(parsed.hostname)
            for net in cls.BLOCKED_NETWORKS:
                if ip in net:
                    raise AppException(ErrorCode.VALIDATION, f"blocked network: {ip}")
        except ValueError:
            pass  # 域名，后续 DNS 解析后再次校验
        return url

    @classmethod
    def validate_headers(cls, headers: dict) -> dict:
        """校验 headers 中的 secret 引用。"""
        for k, v in headers.items():
            if isinstance(v, str) and cls.PLAINTEXT_CRED_PATTERN.search(v):
                raise AppException(ErrorCode.VALIDATION, f"plaintext credential in header: {k}")
            if k.lower() in ("authorization", "x-api-key") and not cls.SECRET_REF_PATTERN.match(str(v)):
                raise AppException(ErrorCode.VALIDATION, f"header must use secret reference: {k}")
        return headers

    @classmethod
    def resolve_secret(cls, ref: str) -> str:
        """解析 ${env:X} / ${vault:path#key} 引用。"""
        if ref.startswith("${env:"):
            env_name = ref[6:-1]
            return os.environ.get(env_name, "")
        elif ref.startswith("${vault:"):
            # vault:path#key → 调用 vault client
            ...
        return ref

# collectors/_field_mapping.py
class FieldMapper:
    """把第三方响应字段映射到 Signal 模型。"""
    @staticmethod
    def map_metric(row: dict, mapping: dict, tenant_id: str) -> MetricSignal:
        return MetricSignal(
            kind="metric",
            tenant_id=tenant_id,
            service=row.get(mapping.get("service", "service"), "unknown"),
            metric=row.get(mapping["metric"]),
            value=float(row.get(mapping["value"]) if isinstance(mapping["value"], str)
                       else _extract_path(row, mapping["value"])),  # 支持 value[1]
            timestamp=_parse_ts(row.get(mapping["timestamp"])),
            labels=row.get("labels", {}),
        )

    @staticmethod
    def map_log(row: dict, mapping: dict, tenant_id: str) -> LogSignal:
        return LogSignal(
            kind="log",
            tenant_id=tenant_id,
            service=row.get(mapping.get("service", "service"), "unknown"),
            level=row.get(mapping["level"]),
            message=row.get(mapping["message"]),
            stack_trace=row.get(mapping.get("stack_trace")),
            timestamp=_parse_ts(row.get(mapping["timestamp"])),
        )

# collectors/http_metrics.py
class HttpMetricsCollector(Collector):
    name = "http_metrics"

    def __init__(self, http: SharedHttpClient, gateway: OutboundGateway):
        self.http = http
        self.gateway = gateway

    async def collect(self, ctx: "DetectionContext", target: dict) -> list:
        sc = target["source_config"]
        url = self.gateway.validate_url(sc["url"])
        headers = self.gateway.validate_headers(sc.get("headers", {}))
        resolved = {k: self.gateway.resolve_secret(v) for k, v in headers.items()}

        # 水位线下推
        watermark = await ctx.watermark_store.get(
            ctx.tenant_id, target["target_id"]
        )
        params = dict(sc.get("params", {}))
        if watermark and watermark.get("last_ts"):
            params["start"] = watermark["last_ts"].isoformat()

        resp = await self.http.request(
            sc.get("method", "GET"), url, headers=resolved, params=params
        )
        resp.raise_for_status()
        rows = resp.json().get("data", {}).get("result", [])
        mapping = sc["field_mapping"]

        signals = []
        seen_hashes = set()
        for row in rows:
            sig = FieldMapper.map_metric(row, mapping, ctx.tenant_id)
            # 幂等去重
            sig_hash = hashlib.md5(
                f"{sig.metric}|{sig.value}|{sig.timestamp}".encode()
            ).hexdigest()
            if sig_hash in seen_hashes:
                continue
            seen_hashes.add(sig_hash)
            signals.append(sig)

        # 更新水位线
        if signals:
            latest_ts = max(s.timestamp for s in signals)
            await ctx.watermark_store.update(
                ctx.tenant_id, target["target_id"], latest_ts
            )

        # 写入 signal_snapshot
        await ctx.snapshot_store.write(
            ctx.tenant_id, target["target_id"], signals
        )

        return signals

def build(*, http=None, pool=None, settings=None):
    return HttpMetricsCollector(http, OutboundGateway())

# collectors/http_logs.py
class HttpLogsCollector(Collector):
    name = "http_logs"

    async def collect(self, ctx, target):
        # 类似 http_metrics，额外：
        # 1. 日志按事件时间戳水位线
        # 2. 堆栈抽取与 signature() 预计算
        ...
        for log_signal in signals:
            log_signal._signature = signature(log_signal, n_frames=3)
        return signals

# collectors/mock.py
class MockCollector(Collector):
    name = "mock"
    async def collect(self, ctx, target):
        # 返回预定义信号，供测试/演示
        return target.get("_mock_signals", [])
```

### Use Case 清单与流程

#### UC-3.1 新增监控端点

```
前置: 用户已登录，有对应租户权限
流程:
  1. 用户在前端「监控管理 > 新建端点」填写表单
  2. 前端校验 url 格式、field_mapping 必填项
  3. POST /v1/monitors（X-Tenant-Id 头）
  4. 后端鉴权中间件校验 tenant 权限
  5. OutboundGateway.validate_url(source_config.url) → 校验 scheme/私网
  6. OutboundGateway.validate_headers(source_config.headers) → 校验 secret 引用
  7. MonitorTargetStore.create(tenant_id, target) → 生成 target_id (MT-NNNN)
  8. 返回 201 + {target_id: "MT-0001"}
  9. scheduler 下一 tick 自动发现新端点并调度
断言: monitor_target 表新增一行；url 通过网关校验；target_id 唯一
```

#### UC-3.2 测试采集连通性

```
前置: 监控端点已创建或正在创建
流程:
  1. 用户在「端点详情」页点击「测试连通性」
  2. 前端 POST /v1/monitors/{target_id}/test（或用 source_config 内联测试）
  3. 后端获取 target 配置
  4. Gateway.validate_url + validate_headers
  5. Collector.collect(ctx, target) 执行一次采集（不写水位线/快照）
  6. 返回采集结果摘要：信号数、字段映射预览、耗时、错误信息
  7. 前端展示 TestResult 面板
断言: 成功时返回信号样本；失败时返回结构化错误（SSRF/超时/字段缺失）
```

#### UC-3.3 指标采集（Prometheus API）

```
前置: Prometheus 指标端点已配置
流程:
  1. scheduler 到期触发 run_round(targets=[MT-0002])
  2. HttpMetricsCollector.collect(ctx, target)
  3. 读取 collect_watermark → last_ts
  4. 下推时间窗参数 params["start"] = last_ts
  5. 发送 HTTP GET/POST 到 Prometheus API
  6. 解析响应 data.result[]，每行通过 field_mapping 映射为 MetricSignal
  7. 按 (metric, value, timestamp) hash 去重
  8. 更新 collect_watermark → last_ts = max(timestamp)
  9. 写入 signal_snapshot
断言: 信号数为 Prometheus 返回条目数（去重后）；watermark 推进；snapshot 写入
```

#### UC-3.4 日志采集（HTTP API）

```
前置: 日志采集端点已配置
流程:
  1. scheduler 到期触发
  2. HttpLogsCollector.collect(ctx, target)
  3. 读取 watermark → last_ts
  4. 下推时间窗 params
  5. 发送 HTTP 请求获取日志
  6. 每条日志通过 field_mapping 映射为 LogSignal
  7. 对每条日志预计算 signature（异常类型 + 顶部 N 帧）
  8. 按 (service, signature, timestamp) hash 去重
  9. 更新 watermark；写入 snapshot
断言: 日志信号数 = 去重后条数；每条 LogSignal 携带 _signature 预计算值
```

#### UC-3.5 水位线推进与幂等去重

```
前置: 同一稳定源连续两轮采集
流程:
  1. 第一轮采集：watermark 为空 → 拉取全量 → 100 条信号 → 写入 watermark
  2. 第二轮采集：watermark = last_ts → 下推 start=last_ts
  3. 第三方 API 返回 0 条新信号（时间窗内无新数据）
  4. signals = []，返回空列表
断言: 第二轮 signals 为空（水位线生效）；watermark 未回退
```

#### UC-3.6 采集源超时降级

```
前置: 第三方 API 响应超过 outbound_timeout_sec
流程:
  1. Collector.collect() 发送请求
  2. httpx 超时抛 TimeoutException
  3. asyncio.gather(return_exceptions=True) 捕获
  4. ctx.degraded_sources.append(target_id)
  5. 其余 source 继续采集
  6. 后续 L0-L3 正常执行（该 source 的信号为空）
断言: 服务不崩溃；degraded_sources 包含该 target_id；其余 source 正常
```

#### UC-3.7 SSRF 拦截

```
前置: 用户提交恶意 URL
流程:
  1. 用户在表单输入 url = "http://169.254.169.254/latest/meta-data/"
  2. 前端提交 POST /v1/monitors
  3. OutboundGateway.validate_url() 解析 hostname
  4. ipaddress.ip_address("169.254.169.254") ∈ 169.254.0.0/16（链路本地）
  5. 抛 AppException(VALIDATION, "blocked network: 169.254.169.254")
  6. 返回 400 + {code: "VALIDATION_ERROR", reason: "blocked network..."}
断言: 请求被拒绝；monitor_target 表无新增；同样拦截 127.0.0.1 / 10.x / 192.168.x
```

#### UC-3.8 Secret 引用解析

```
前置: source_config.headers = {"Authorization": "Bearer ${env:ORDER_TOKEN}"}
流程:
  1. validate_headers 检查 "${env:ORDER_TOKEN}" → 匹配 SECRET_REF_PATTERN
  2. 采集时 resolve_secret("${env:ORDER_TOKEN}") → os.environ["ORDER_TOKEN"]
  3. 实际请求使用解析后的值
  4. 如果提交明文 "Bearer abc123" → PLAINTEXT_CRED_PATTERN 命中 → 拒绝
断言: secret 引用可解析；明文凭据被拒绝；env 变量不存在时返回空字符串
```

---

## M4 — 插件化（registry + 内置 detector/suppressor）（已实现）

> **已实现**（2026-08-26）。实现日志见 [`docs/logs/M4.md`](../logs/M4.md)，历史规格归档见 [`docs/archive/M4-plugins.md`](../archive/M4-plugins.md)，实现计划见 [`docs/plans/M4-implementation-plan.md`](../plans/M4-implementation-plan.md)。剩余项：漏斗主体（`l0_suppress`/`l1_detect`/L2/L3/emit）随 M5；维护窗口/黑名单表读取与 admin 写表 API 随 M6；reload admin 权限随 M7。

### 基础信息

- **目标**：真插件系统可用，内置 detector/suppressor 全部实现
- **依赖**：M1（契约 + `build()` 注入）、M3（共享 http/pool）
- **关键修正**：P1#11/14/16/17/18/23 在此落地
- **完成标准**：插件契约测试全通过；`reload` 期间跑一轮不抛异常；`filter_signals` 结构化 matcher 全分支覆盖

### 前端菜单与页面

| 菜单路径 | 页面 | 组件 | 说明 |
|---------|------|------|------|
| 系统管理 > 插件管理 | PluginListPage | `PluginTable` 表格 + `ReloadButton` | 列出所有已加载插件（kind/name/status）；按 collector/detector/suppressor 分组展示；一键重新加载 |
| 配置管理 > 检测规则 | DomainConfigPage | `ConfigEditor` JSON 编辑器 + `ConfigForm` | 编辑 domain_config 的 detectors/suppressors/correlation/verify JSON；支持从插件列表选择插件名 |
| 配置管理 > 规则版本 | ConfigVersionPage | `VersionHistory` 时间线 | 展示 domain_config 版本历史、修改人、变更 diff |

**前端交互流程**：
- 插件管理页：调用 `GET /v1/plugins` 获取列表 → 按分组展示 → `POST /v1/plugins/reload` 触发重载
- 检测规则页：调用 `GET /v1/config/{domain}` 获取当前规则 → 编辑 JSON → `PUT /v1/config/{domain}` 保存（需 admin 权限）
- 规则编辑器：从插件列表拉取可用 detector/suppressor 名，作为下拉选项

### 后端实现骨架

```python
# plugins/registry.py
from types import MappingProxyType
import importlib.metadata as m

GROUPS = {
    "collector":  "aiops_apm.collectors",
    "detector":   "aiops_apm.detectors",
    "suppressors": "aiops_apm.suppressors",
}

class PluginRegistry:
    def __init__(self):
        self._active: dict[str, dict[str, Plugin]] = {k: {} for k in GROUPS}

    def load(self, *, http=None, pool=None, settings=None):
        """启动时遍历三组 entry_points，实例化并注册。"""
        snapshot = {k: {} for k in GROUPS}
        for kind, group in GROUPS.items():
            for ep in m.entry_points(group=group):
                try:
                    factory = ep.load()
                    plugin = factory(http=http, pool=pool, settings=settings)
                    snapshot[kind][ep.name] = plugin
                except Exception as e:
                    logger.warning("plugin load failed", group=group, name=ep.name, err=e)
        self._active = MappingProxyType(snapshot)  # 不可变快照
        return self

    def reload(self, *, http=None, pool=None, settings=None):
        """重新发现插件（产出新快照后原子替换）。"""
        # 构建新快照（不修改 _active）
        new_snapshot = ...
        self._active = MappingProxyType(new_snapshot)  # 一次原子替换
        return self

    def get(self, kind: str, name: str) -> Plugin:
        table = self._active.get(kind, {})
        if name not in table:
            raise AppException(ErrorCode.PLUGIN_NOT_FOUND, f"{kind}/{name}")
        return table[name]

    def list(self, kind: str | None = None) -> dict:
        if kind:
            return {kind: list(self._active.get(kind, {}).keys())}
        return {k: list(v.keys()) for k, v in self._active.items()}

# detectors/static_threshold.py
from enum import Enum

class Operator(str, Enum):
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    RANGE = "range"

class StaticThresholdDetector(Detector):
    name = "static_threshold"

    async def detect(self, signals: list, params: dict) -> list:
        threshold = params["threshold"]
        operator = Operator(params.get("operator", "gt"))
        anomalies = []
        for s in signals:
            if not isinstance(s, MetricSignal):
                continue
            hit = False
            if operator == Operator.GT:
                hit = s.value > threshold
            elif operator == Operator.GTE:
                hit = s.value >= threshold
            elif operator == Operator.LT:
                hit = s.value < threshold
            elif operator == Operator.LTE:
                hit = s.value <= threshold
            elif operator == Operator.RANGE:
                lo, hi = params.get("range", [threshold, threshold])
                hit = not (lo <= s.value <= hi)
            if hit:
                anomalies.append(MetricAnomaly(
                    kind="metric",
                    tenant_id=s.tenant_id,
                    service=s.service,
                    metric=s.metric,
                    value=s.value,
                    method=self.name,
                    severity=params.get("severity", "warning"),
                    detected_at=s.timestamp,
                    labels=s.labels,
                ))
        return anomalies

def build(*, http=None, pool=None, settings=None):
    return StaticThresholdDetector()

# detectors/simple_compare.py
class SimpleCompareDetector(Detector):
    name = "simple_compare"

    async def detect(self, signals, params):
        ratio = params.get("ratio", 1.5)
        # 基线从 signal_snapshot 取滚动均值（修正 P1#18）
        baseline = params.get("baseline")
        anomalies = []
        for s in signals:
            if not isinstance(s, MetricSignal):
                continue
            # 如果有 snapshot_store，从历史取滚动均值
            # if ctx.snapshot_store:
            #     hist = await ctx.snapshot_store.read_window(...)
            #     baseline = mean(hist)
            if baseline and s.value > baseline * ratio:
                anomalies.append(MetricAnomaly(
                    kind="metric", tenant_id=s.tenant_id,
                    service=s.service, metric=s.metric,
                    value=s.value, baseline=baseline,
                    method=self.name,
                    severity=params.get("severity", "warning"),
                    detected_at=s.timestamp,
                ))
        return anomalies

def build(*, http=None, pool=None, settings=None):
    return SimpleCompareDetector()

# detectors/signature_aggregate.py
class SignatureAggregateDetector(Detector):
    name = "signature_aggregate"

    async def detect(self, signals, params):
        min_count = params.get("min_count", 5)
        n_frames = params.get("n_frames", 3)
        # 按 signature 分组
        groups: dict[str, list[LogSignal]] = {}
        for s in signals:
            if not isinstance(s, LogSignal):
                continue
            sig = signature(s, n_frames)
            groups.setdefault(sig, []).append(s)
        anomalies = []
        for sig, logs in groups.items():
            if len(logs) >= min_count:
                anomalies.append(LogAnomaly(
                    kind="log", tenant_id=logs[0].tenant_id,
                    service=logs[0].service, level=logs[0].level,
                    signature=sig, pattern=logs[0].message[:120],
                    count=len(logs), first_seen=min(l.timestamp for l in logs),
                    severity=params.get("severity", "warning"),
                    detected_at=max(l.timestamp for l in logs),
                ))
        return anomalies

def build(*, http=None, pool=None, settings=None):
    return SignatureAggregateDetector()

# suppressors/maintenance_window.py
class MaintenanceWindowSuppressor(Suppressor):
    name = "maintenance_window"

    async def check(self, signal, ctx, params) -> str | None:
        for w in ctx.maintenance_windows:
            if (w["service"] == signal.service
                and w["start_at"] <= signal.timestamp <= w["end_at"]):
                return f"maintenance_window: {w.get('reason', '')}"
        return None

    async def batch_check(self, signals, ctx, params):
        # 一次 query 取回窗口后内存匹配
        windows = ctx.maintenance_windows
        results = []
        for s in signals:
            reason = None
            for w in windows:
                if w["service"] == s.service and w["start_at"] <= s.timestamp <= w["end_at"]:
                    reason = f"maintenance_window: {w.get('reason', '')}"
                    break
            results.append((s, reason))
        return results

def build(*, http=None, pool=None, settings=None):
    return MaintenanceWindowSuppressor()

# suppressors/blacklist.py
class BlacklistSuppressor(Suppressor):
    name = "blacklist"

    async def batch_check(self, signals, ctx, params):
        bl = ctx.blacklist  # list of {domain, service, signal}
        results = []
        for s in signals:
            reason = None
            for entry in bl:
                if entry["service"] == s.service:
                    if isinstance(s, MetricSignal) and entry["signal"] == s.metric:
                        reason = f"blacklist: {entry.get('reason', '')}"
                        break
                    elif isinstance(s, LogSignal) and entry["signal"] == s.level:
                        reason = f"blacklist: {entry.get('reason', '')}"
                        break
            results.append((s, reason))
        return results

def build(*, http=None, pool=None, settings=None):
    return BlacklistSuppressor()

# pipeline/filter_signals.py —— 结构化 matcher（修正 P1#11）
def filter_signals(
    signals: list, matcher: str | dict | None
) -> list:
    """结构化信号匹配。"""
    if matcher is None or matcher == "*":
        return signals
    if isinstance(matcher, str):
        # 向后兼容：字符串视为 metric 名或 log level
        return [s for s in signals
                if (isinstance(s, MetricSignal) and s.metric == matcher)
                or (isinstance(s, LogSignal) and s.level == matcher)]
    if isinstance(matcher, dict):
        result = []
        for s in signals:
            if matcher.get("signal_type") == "metric" and isinstance(s, MetricSignal):
                if s.metric == matcher.get("metric") or not matcher.get("metric"):
                    if _labels_match(s.labels, matcher.get("labels", {})):
                        if not matcher.get("service") or s.service == matcher["service"]:
                            result.append(s)
            elif matcher.get("signal_type") == "log" and isinstance(s, LogSignal):
                if not matcher.get("level") or s.level == matcher.get("level"):
                    if not matcher.get("service") or s.service == matcher["service"]:
                        result.append(s)
        return result
    return []
```

### Use Case 清单与流程

#### UC-4.1 查看已加载插件列表

```
前置: 服务已启动，插件已加载
流程:
  1. 用户打开「系统管理 > 插件管理」
  2. 前端 GET /v1/plugins（X-Tenant-Id）
  3. PluginRegistry.list() 返回 {collector: [...], detector: [...], suppressor: [...]}
  4. 前端按分组渲染 PluginTable
断言: 内置 3 个 collector、3 个 detector、2 个 suppressor 均在列表中
```

#### UC-4.2 重新加载插件

```
前置: 新安装了第三方插件包
流程:
  1. 用户点击「重新加载」按钮
  2. 前端 POST /v1/plugins/reload（需 admin 权限）
  3. PluginRegistry.reload() 构建新快照
  4. 原子替换 self._active = MappingProxyType(new_snapshot)
  5. 正在执行的轮次继续用旧快照
  6. 下一轮起使用新快照
  7. 返回更新后的插件列表
断言: 新插件出现在列表中；正在执行的轮次不受影响；无竞态异常
```

#### UC-4.3 配置静态阈值检测器

```
前置: domain_config 可编辑
流程:
  1. 用户在「检测规则」页编辑 domain=application 的 config
  2. 在 detectors 数组添加: {"signal": {"signal_type":"metric","metric":"cpu_usage"}, "plugin":"static_threshold", "params":{"threshold":0.9,"operator":"gt"}, "severity":"high"}
  3. 保存 → PUT /v1/config/application
  4. DomainConfigStore.upsert 校验 DomainConfig 模型
  5. version 递增
断言: domain_config 表 config 列更新；version 递增；校验失败抛 ConfigValidationError
```

#### UC-4.4 配置环比检测器

```
前置: signal_snapshot 表有历史数据
流程:
  1. 添加 detector: {"signal": {"signal_type":"metric","metric":"error_rate"}, "plugin":"simple_compare", "params":{"ratio":1.5}, "severity":"high"}
  2. 运行时 SimpleCompareDetector 从 signal_snapshot 取滚动均值作为 baseline
  3. 当前值 > baseline * ratio → 生成 MetricAnomaly
断言: baseline 从 snapshot 取（非硬编码）；ratio 正确应用
```

#### UC-4.5 配置签名聚合检测器

```
前置: 日志采集端点已配置
流程:
  1. 添加 detector: {"signal": {"signal_type":"log","level":"ERROR"}, "plugin":"signature_aggregate", "params":{"min_count":5,"n_frames":3}, "severity":"warning"}
  2. 采集到 47 条相同 OOM 堆栈的 LogSignal
  3. signature() 提取 "OutOfMemoryError|com.app.Service.method"
  4. 按 signature 分组，count=47 >= min_count=5
  5. 生成 1 条 LogAnomaly(count=47)
断言: 47 条 → 1 条 anomaly；signature 稳定可复现；count=47
```

#### UC-4.6 配置维护窗口抑制器

```
前置: maintenance_window 表有记录
流程:
  1. domain_config.suppressors 添加: {"name": "maintenance_window"}
  2. maintenance_window 表插入: {service:"order-management", start_at:"...", end_at:"...", reason:"deploy"}
  3. 信号 timestamp 落在窗口内 → MaintenanceWindowSuppressor.check 返回 reason
  4. 信号被 L0 抑制
断言: 窗口内信号被抑制；窗口外信号放行；suppressed 审计有记录
```

#### UC-4.7 配置黑名单抑制器

```
前置: suppress_blacklist 表有记录
流程:
  1. domain_config.suppressors 添加: {"name": "blacklist"}
  2. suppress_blacklist 表插入: {domain:"application", service:"order-management", signal:"cpu_usage", reason:"known noise"}
  3. 信号 service+metric 命中黑名单 → 抑制
断言: 命中信号被抑制；未命中信号放行；batch_check 一次查询
```

#### UC-4.8 第三方插件安装与发现

```
前置: 第三方插件包已 pip install
流程:
  1. 第三方包在 pyproject.toml 声明 [project.entry-points."aiops_apm.detectors"] p95_latency = "latency_detector:build"
  2. POST /v1/plugins/reload
  3. PluginRegistry.reload() 遍历 entry_points
  4. 发现 p95_latency 插件并实例化
  5. 在 domain_config 中引用 {"plugin": "p95_latency", ...}
  6. 下一轮检测自动使用
断言: 新插件出现在 /v1/plugins 列表；可在 domain_config 引用；检测生效
```

---

## M5 — 漏斗 L0–L3 + emit（确定性核心）

> **已实现**（2026-08-26）。实现日志见 [`docs/logs/M5.md`](../logs/M5.md)，历史规格归档见 [`docs/archive/M5-funnel.md`](../archive/M5-funnel.md)，实现计划见 [`docs/plans/M5-implementation-plan.md`](../plans/M5-implementation-plan.md)。剩余项：用例 2（内存泄漏组合升 critical）端到端与 scheduler/API 随 M6；LLM L2 摘要、fpr 回写随 M6/v2。

### 基础信息

- **目标**：一个 `(tenant_id, domain)` 内的完整漏斗可独立运行（暂不接 scheduler）
- **依赖**：M1（契约 + fingerprint）、M2（store + `detection_state` 持久化）、M4（detector/suppressor 插件）
- **关键修正**：P0#1/#2/#3/#7/#8/#9 在此落地；这是**最高风险阶段**，建议先用 §13 用例做 TDD 驱动
- **完成标准**：§13 用例 1/3/4/5/6/7/8/9/10/11 全部通过（用 InMemoryStore + mock collector）

### 前端菜单与页面

| 菜单路径 | 页面 | 组件 | 说明 |
|---------|------|------|------|
| 告警管理 > 问题列表 | ProblemListPage | `ProblemTable` 表格 + `FilterBar` + `SeverityBadge` | 列出 problem_record；筛选：state（pending/resolved/closed）、severity（warning/high/critical）、service；分页 |
| 告警管理 > 问题详情 | ProblemDetailPage | `ProblemDetail` 面板 + `AnomalyList` + `EvidenceTimeline` + `CorrelationView` | 展示：record_id、severity、service、detected_at、symptom.summary、metric/log anomalies、correlation、verification、evidence 时间线、trace_id |
| 监控管理 > 检测状态 | DetectionStatePage | `StateTable` 表格 | 展示 detection_state：anomaly_key → consecutive_rounds / miss_rounds / first_seen / last_seen；用于排查"为什么还没开单" |

**前端关键交互**：
- 问题列表：`GET /v1/problems?state=pending&severity=high&limit=50` → 渲染表格
- 问题详情：`GET /v1/problems/{record_id}` → 渲染详情面板
- 检测状态：`GET /v1/detection-state?domain=application` → 展示各 anomaly 的持续性计数
- Severity 颜色映射：critical（红）、high（橙）、warning（黄）

### 后端实现骨架

```python
# pipeline/context.py —— 修正 P0#7
from dataclasses import dataclass, field
from datetime import datetime

@dataclass(kw_only=True)  # 修正字段顺序问题
class DetectionContext:
    trace_id: str
    tenant_id: str = "default"
    domain: str
    domain_config: dict
    registry: "PluginRegistry"
    storage: "RecordStore"
    now: datetime

    # 本轮数据
    targets: list[dict] = field(default_factory=list)
    signals: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    suppressed: list = field(default_factory=list)
    anomalies: list = field(default_factory=list)

    # 动态配置
    maintenance_windows: list = field(default_factory=list)
    blacklist: list = field(default_factory=list)
    fpr: dict = field(default_factory=dict)

    # 跨轮状态
    previous_keys: set = field(default_factory=set)

    # 修正补齐字段
    degraded_sources: list = field(default_factory=list)
    round_started_at: datetime | None = None
    target_map: dict[str, dict] = field(default_factory=dict)
    snapshots: dict[str, list] = field(default_factory=dict)

    # 依赖注入的 store
    watermark_store: "WatermarkStore | None" = None
    snapshot_store: "SnapshotStore | None" = None
    state_store: "DetectionStateStore | None" = None
    sequence_store: "SequenceStore | None" = None

# pipeline/l0_suppress.py
async def l0_suppress(ctx: DetectionContext) -> None:
    """L0 抑制：批量检查，记录审计。"""
    kept, suppressed = [], []
    for sc in ctx.domain_config.get("suppressors", []):
        sup = ctx.registry.get("suppressor", sc["name"])
        results = await sup.batch_check(ctx.signals, ctx, sc.get("params", {}))
        for signal, reason in results:
            if reason and signal not in suppressed:
                suppressed.append({"signal": signal, "reason": reason, "suppressor": sc["name"]})
                ctx.suppressed = suppressed
                # Prometheus 计数器
                # counter.labels(suppressor=sc["name"]).inc()
    kept = [s for s in ctx.signals
            if not any(s is item["signal"] for item in suppressed)]
    ctx.signals = kept

# pipeline/l1_detect.py
async def l1_detect(ctx: DetectionContext) -> None:
    """L1 检测：按 detector 配置分发。"""
    for dc in ctx.domain_config.get("detectors", []):
        detector = ctx.registry.get("detector", dc["plugin"])
        matched = filter_signals(ctx.signals, dc.get("signal"))
        if not matched:
            continue
        new_anoms = await detector.detect(matched, dc.get("params", {}))
        for a in new_anoms:
            a.method = detector.name
            # severity 优先级: computed → config → default (修正 P1#12)
            a.severity = dc.get("severity", a.severity)
            ctx.anomalies.append(a)

# pipeline/l2_correlate.py
async def l2_correlate(ctx: DetectionContext) -> tuple:
    """L2 关联：按 service 分组（修正 P0#1）。"""
    from collections import defaultdict
    by_service: dict[str, list] = defaultdict(list)
    for a in ctx.anomalies:
        by_service[a.service].append(a)

    correlations = {}
    for service, anoms in by_service.items():
        metric_anoms = [a for a in anoms if a.kind == "metric"]
        log_anoms = [a for a in anoms if a.kind == "log"]

        related, reason = False, "single-source"
        if metric_anoms and log_anoms:
            window = ctx.domain_config["correlation"].get("metric_log_window_sec", 300)
            same_svc = True  # 已按 service 分组
            within = _within_window(metric_anoms, log_anoms, window)
            if same_svc and within:
                related, reason = True, "metric_log_within_window"
            else:
                reason = "unrelated"
        elif metric_anoms and not log_anoms:
            reason = "metric_only"
        elif log_anoms and not metric_anoms:
            reason = "log_only"

        # 变更关联
        change_related = _change_within_window(
            ctx.changes, anoms,
            ctx.domain_config["correlation"].get("change_window_sec", 300)
        )

        correlations[service] = (Correlation(related=related, reason=reason), change_related)

    return correlations

def template_summary(metric_anoms, log_anoms) -> str:
    """LLM 兜底模板摘要。"""
    parts = []
    for a in metric_anoms:
        parts.append(f"{a.service} {a.metric} {a.value}")
    for a in log_anoms:
        parts.append(f"{a.service} {a.signature} x{a.count}")
    return "；".join(parts)

# pipeline/l3_verify.py
async def l3_verify(ctx: DetectionContext, service: str,
                    anomalies: list) -> Verification:
    """L3 验证：改为过滤器（修正 P0#2/#8）。"""
    vc = ctx.domain_config.get("verify", {})
    persistence_rounds = vc.get("persistence_rounds", 2)
    fpr_threshold = vc.get("false_positive_threshold", 0.6)
    min_samples = vc.get("min_samples", 20)

    # 持续性过滤（修正 P0#2）
    persisted = []
    for a in anomalies:
        key = fingerprint.anomaly_key(a)
        state = await ctx.state_store.get(ctx.tenant_id, ctx.domain, key)
        consecutive = state["consecutive_rounds"] if state else 0
        if consecutive >= persistence_rounds:
            persisted.append(a)
        # 更新 detection_state
        await ctx.state_store.upsert(
            ctx.tenant_id, ctx.domain, key,
            first_seen=state["first_seen"] if state else ctx.now,
            last_seen=ctx.now,
            consecutive_rounds=consecutive + 1,
            miss_rounds=0,
        )

    if not persisted:
        return Verification(passed=False, persistence_ok=False,
                          resample_ok=True, false_positive_rate=0.0,
                          final_severity="warning")

    # 误报率闸门（修正 P0#8）
    gk = fingerprint.group_key(ctx.tenant_id, ctx.domain, service, persisted)
    fpr_entry = ctx.fpr.get(gk, {"fpr": 0.0, "total": 0})
    fpr = fpr_entry.get("fpr", 0.0)
    total = fpr_entry.get("total", 0)
    fpr_ok = total < min_samples or fpr < fpr_threshold
    # fpr 命中只降级 severity，不直接丢弃
    if not fpr_ok:
        severity = "warning"  # 降级
    else:
        severity = calibrate_severity(persisted)

    return Verification(
        passed=True,
        persistence_ok=True,
        resample_ok=True,
        false_positive_rate=fpr,
        final_severity=severity,
    )

# pipeline/emit.py
async def emit(ctx: DetectionContext, service: str,
               anomalies: list, correlation: Correlation,
               change_related: bool, verification: Verification) -> list:
    """emit：用 fingerprint 真源 + 原子去重（修正 P0#3/#9）。"""
    if not verification.passed:
        return []

    gk = fingerprint.group_key(ctx.tenant_id, ctx.domain, service, anomalies)
    metric_anoms = [a for a in anomalies if a.kind == "metric"]
    log_anoms = [a for a in anomalies if a.kind == "log"]

    record_id = await ctx.sequence_store.next_id(ctx.domain)
    rec = ProblemRecord(
        record_id=record_id,
        tenant_id=ctx.tenant_id,
        domain=ctx.domain,
        state="pending",
        service=service,
        severity=verification.final_severity,
        detected_at=ctx.now,
        first_seen_at=ctx.now,
        last_seen_at=ctx.now,
        occurrence_count=1,
        symptom={"summary": template_summary(metric_anoms, log_anoms)},
        metric_anomalies=metric_anoms,
        log_anomalies=log_anoms,
        correlation=correlation,
        change_related=change_related,
        verification=verification,
        evidence=[],
        trace_id=ctx.trace_id,
    )
    # 原子去重（修正 P0#3）：INSERT ... ON DUPLICATE KEY UPDATE
    actual_id = await ctx.storage.write_or_append(ctx.tenant_id, rec)
    return [rec] if actual_id == record_id else []

# pipeline/runner.py
async def run_domain(ctx: DetectionContext) -> "DomainResult":
    """串行执行 collect → L0 → L1 → L2 → L3 → emit。"""
    ctx.round_started_at = ctx.now
    timeline = []

    # collect（已在 ctx.signals 中，由 poller 填充）
    timeline.append({"step": "collect_done", "ts": ctx.now, "count": len(ctx.signals)})

    # L0
    await l0_suppress(ctx)
    timeline.append({"step": "suppressed", "count": len(ctx.suppressed)})

    # L1
    await l1_detect(ctx)
    timeline.append({"step": "detected", "count": len(ctx.anomalies)})

    # L2
    correlations = await l2_correlate(ctx)
    timeline.append({"step": "correlated", "services": list(correlations.keys())})

    # L3 + emit（按 service 分组）
    records = []
    from collections import defaultdict
    by_service = defaultdict(list)
    for a in ctx.anomalies:
        by_service[a.service].append(a)

    for service, anoms in by_service.items():
        corr, change_related = correlations[service]
        verification = await l3_verify(ctx, service, anoms)
        new_records = await emit(ctx, service, anoms, corr, change_related, verification)
        records.extend(new_records)

    timeline.append({"step": "record_created", "count": len(records)})

    return DomainResult(
        domain=ctx.domain,
        records=records,
        suppressed_count=len(ctx.suppressed),
        anomaly_count=len(ctx.anomalies),
        degraded_sources=ctx.degraded_sources,
        timeline=timeline,
    )
```

### Use Case 清单与流程（映射 §13）

#### UC-5.1 用例 1：CPU 飙高两轮

```
前置: static_threshold detector(threshold=0.9) 配置；persistence_rounds=2
流程:
  1. 第一轮: MetricSignal(cpu_usage=0.91) 采集
  2. L0: 无抑制 → 放行
  3. L1: static_threshold 检测 0.91 > 0.9 → MetricAnomaly(severity=high)
  4. L2: 仅指标 → correlation.related=false, reason="metric_only"
  5. L3: detection_state 中该 anomaly_key consecutive_rounds=1 < 2 → 不通过
  6. emit: verification.passed=false → 不开单
  7. detection_state 更新: consecutive_rounds=1
  8. 第二轮: 同样信号
  9. L1: 同样 anomaly
 10. L3: detection_state consecutive_rounds=2 >= 2 → 通过
 11. emit: 开单 ProblemRecord(severity=high, metric_anomalies=[cpu])
断言: 第一轮不生成 record；第二轮生成 1 条 record；severity=high
```

#### UC-5.2 用例 2：内存泄漏 + Full GC 组合

```
前置: static_threshold(heap) + signature_aggregate(Full GC) 配置
流程:
  1. 采集: heap 指标递增 + 47 条 Full GC 日志
  2. L1: static_threshold → MetricAnomaly(heap, high)
         signature_aggregate → LogAnomaly(FullGC, count=47, high)
  3. L2: 同 service + 时间窗内 → correlation.related=true
  4. L3: 持续性通过（第二轮）；组合信号 heap高 + FullGC突增 → severity=critical
  5. emit: 1 条 ProblemRecord(severity=critical, metric+log anomalies)
断言: severity=critical；correlation.related=true；包含 metric + log anomalies
```

#### UC-5.3 用例 3：47 条 OOM 日志聚合

```
前置: signature_aggregate(min_count=5) 配置；无指标信号
流程:
  1. 采集: 47 条 ERROR 级日志，相同堆栈签名
  2. L1: signature_aggregate 按 signature 分组 → count=47 >= 5 → 1 条 LogAnomaly
  3. L2: 仅日志 → reason="log_only"
  4. L3: 持续性通过
  5. emit: 纯日志开单 ProblemRecord(log_anomalies=[OOM], count=47)
断言: 47 条 → 1 条 anomaly；纯日志开单；count=47
```

#### UC-5.4 用例 4：指标+日志同源关联（修正 P0#1）

```
前置: MT-0001(日志) + MT-0002(指标) 同属 service=order-management
流程:
  1. scheduler 按 (tenant_id, domain) 聚合两个 target 到同一 context
  2. 采集: 指标 connection_pool=0.98 + 日志 ConnectionRefused 20 条
  3. L1: static_threshold → MetricAnomaly(connection_pool)
         signature_aggregate → LogAnomaly(ConnectionRefused, count=20)
  4. L2: 按 service 分组 → 同 service + 时间窗内 → related=true
  5. L3: 持续性通过
  6. emit: 1 条 ProblemRecord（合并指标+日志），correlation.related=true
断言: 只有 1 条 record（不是 2 条）；related=true；包含 metric + log anomalies
      ★ 修正前: run_round 为每个 target 建独立 context → metric_anoms/log_anoms 恒不同时为真 → 永远走不到 related
```

#### UC-5.5 用例 5：错误率突增 + 部署变更

```
前置: change_record 有部署记录；simple_compare(error_rate) 配置
流程:
  1. 采集: error_rate=0.08（baseline=0.02, ratio=1.5 → 0.08 > 0.03）
  2. L1: simple_compare → MetricAnomaly(error_rate, high)
  3. L2: 变更关联 → change_record 在 ±300s 内 → change_related=true
  4. L3: 持续性通过
  5. emit: ProblemRecord(change_related=true, recent_change={change_id, summary})
断言: change_related=true；recent_change 包含 change_id + summary
```

#### UC-5.6 用例 6：瞬时抖动过滤（修正 P0#2）

```
前置: persistence_rounds=2；detection_state 持久化
流程:
  1. 第一轮: cpu_usage=0.91 → L1 anomaly → L3 consecutive=1 < 2 → 不通过
  2. detection_state 写入: consecutive_rounds=1
  3. 第二轮: cpu_usage=0.85（正常）→ L1 无 anomaly
  4. detection_state 更新: miss_rounds=1, consecutive_rounds=0
  5. 第三轮: cpu_usage=0.91 → L1 anomaly → L3 consecutive=0 < 2 → 不通过
  6. detection_state 更新: consecutive_rounds=1
断言: 三轮均不开单；detection_state 正确反映 consecutive/miss rounds
      ★ 修正前: 内存 state 进程重启即丢 → 退化为直接开单
```

#### UC-5.7 用例 7：维护窗口抑制

```
前置: maintenance_window 有记录（service=order-management, 时间窗覆盖 now）
流程:
  1. 采集: cpu_usage=0.91 信号
  2. L0: MaintenanceWindowSuppressor.check → 命中窗口 → 返回 reason
  3. 信号被加入 suppressed 列表
  4. L1: 无信号 → 无 anomaly
  5. L3: 无 anomaly → verification.passed=false
  6. emit: 不开单
断言: 不生成 record；suppressed 审计有记录；suppressed_count=1
```

#### UC-5.8 用例 8：误报率闸门（修正 P0#8）

```
前置: fpr_table 有记录(group_key=X, fpr=0.7, total=50)；min_samples=20；fpr_threshold=0.6
流程:
  1. L1 产出 anomaly（同 group_key=X）
  2. L3: 读取 fpr=0.7 > 0.6 且 total=50 >= min_samples=20 → fpr_ok=false
  3. ★ 修正: 只降级 severity 到 warning，不直接丢弃
  4. verification.passed=true, final_severity="warning"
  5. emit: 开单（severity 被降级）
  6. 留下抑制审计记录
断言: 仍然开单（不被永久静默）；severity 被降级到 warning；有审计记录
      ★ 修正前: 1次误报/1次总数=1.0 > 0.6 → 该 group_key 永久静默
```

#### UC-5.9 用例 9：无信号提前终止

```
前置: 采集端点返回空（无信号）
流程:
  1. collect: signals=[]（无新信号）
  2. L0: 无信号 → 无抑制
  3. L1: 无信号 → 无 anomaly
  4. L2: 无 anomaly → 无关联
  5. L3: 无 anomaly → verification.passed=false
  6. emit: 不开单
  7. timeline 记录: collect_done(count=0), detected(count=0)
断言: 不生成 record；零 LLM 调用；timeline 正确记录
```

#### UC-5.10 用例 10：日志源超时降级

```
前置: 日志采集端点响应超过 timeout
流程:
  1. collect: asyncio.gather(return_exceptions=True)
  2. 日志 collector 超时 → TimeoutException 被捕获
  3. ctx.degraded_sources.append(target_id)
  4. 指标 source 正常返回
  5. L0-L3 正常执行（只有指标信号）
  6. emit: 开单（只有 metric_anomalies），symptom 标记 degraded
  7. record 包含 degraded_sources 列表
断言: 服务不崩溃；record 带 degraded 标记；degraded_sources 包含该 target_id
```

#### UC-5.11 用例 11：单条 info 弱信号不升级

```
前置: signature_aggregate(min_count=5) 配置
流程:
  1. 采集: 1 条 INFO 级日志
  2. L1: signature_aggregate 按 signature 分组 → count=1 < 5 → 不生成 anomaly
  3. L3: 无 anomaly → 不通过
  4. emit: 不开单
断言: 不生成 record；1 条弱信号不升级为事件
```

---

## M6 — 调度、多租户、API、恢复闭环

### 基础信息

- **目标**：完整可运行服务：scheduler 自动跑、API 自服务、多租户安全、异常消失自动关单
- **依赖**：M1–M5 全部
- **关键修正**：P0#5/#10/#9、P1#20 在此落地
- **完成标准**：§13 用例 2 端到端通过；两并发轮次同 group_key 不重复开单；reconcile 自动关单；未授权 403；多副本只一个跑调度

### 前端菜单与页面

| 菜单路径 | 页面 | 组件 | 说明 |
|---------|------|------|------|
| 监控管理 > 监控端点（增强） | MonitorListPage | `MonitorTable` + `RunNowButton` + `EnableToggle` | 每行增加「立即执行」按钮和「启用/禁用」开关 |
| 告警管理 > 问题列表（增强） | ProblemListPage | `ProblemTable` + `SeverityFilter` + `StateFilter` | 增加 severity 独立筛选（P1#19）、state 筛选、手动关闭按钮 |
| 告警管理 > 问题详情（增强） | ProblemDetailPage | `ProblemDetail` + `ResolveButton` + `EvidenceList` | 增加「手动关闭」按钮（resolve_reason=manual）；evidence 只展示最近 M 条 |
| 系统管理 > 调度状态 | SchedulerStatusPage | `LeaseHolder` 卡片 + `RunningTasks` 表格 | 展示当前 lease holder、到期时间、在飞任务列表 |
| 配置管理 > 维护窗口 | MaintenanceWindowPage | `WindowTable` + `CreateForm` | CRUD 维护窗口：service、start_at、end_at、reason |
| 配置管理 > 黑名单 | BlacklistPage | `BlacklistTable` + `CreateForm` | CRUD 黑名单：domain、service、signal、reason、enabled |
| 配置管理 > 误报率 | FprPage | `FprTable` | 展示 fpr_table：group_key → fp_cnt/total/fpr/window |
| 手动操作 > 触发检测 | TriggerPage | `RunAllButton` + `RunSingleSelect` | 手动触发全量检测或单个端点检测 |

### 后端实现骨架

```python
# auth/middleware.py —— 修正 P0#5
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

class AuthMiddleware(BaseHTTPMiddleware):
    """API Key/JWT → principal → 允许 tenant 集合。"""
    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/health", "/ready"):
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")
        token = request.headers.get("Authorization")
        tenant_id = request.headers.get("X-Tenant-Id", "default")

        principal = await self._authenticate(api_key, token)
        if not principal:
            return JSONResponse(status_code=401, content={
                "code": "UNAUTHORIZED", "reason": "invalid credentials"
            })

        if tenant_id not in principal.allowed_tenants:
            return JSONResponse(status_code=403, content={
                "code": "PERMISSION_DENIED",
                "reason": f"tenant {tenant_id} not allowed"
            })

        request.state.principal = principal
        request.state.tenant_id = tenant_id
        return await call_next(request)

    async def _authenticate(self, api_key, token) -> "Principal | None":
        # API Key 验证或 JWT 解析
        ...

class Principal:
    def __init__(self, id: str, allowed_tenants: set[str], is_admin: bool = False):
        self.id = id
        self.allowed_tenants = allowed_tenants
        self.is_admin = is_admin

# scheduler.py —— 修正 P0#10
import asyncio
import time
import random

class Scheduler:
    def __init__(self, registry, storage, settings):
        self.registry = registry
        self.storage = storage
        self.settings = settings
        self._next_run: dict[tuple, float] = {}
        self._in_flight: set[tuple] = set()
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_rounds)
        self._lease: SchedulerLease | None = None
        self._running = False

    async def loop(self):
        """主调度循环。"""
        self._running = True
        while self._running:
            try:
                # 多副本选主
                if self._lease and not await self._lease.renew():
                    await asyncio.sleep(self.settings.scheduler_tick_sec)
                    continue

                now = time.monotonic()
                # 载入所有启用的 target
                all_targets = []
                for tenant_id in await self._get_tenants():
                    targets = await self.storage.monitor_targets.list(tenant_id)
                    all_targets.extend(targets)

                # 找出到期的 target
                due = []
                for t in all_targets:
                    key = (t["tenant_id"], t["target_id"])
                    if key in self._in_flight:
                        continue
                    if now >= self._next_run.get(key, 0):
                        due.append(t)
                        # 按计划时间推进（修正 P0#10）
                        interval = t["schedule"].get("interval_sec", 60)
                        jitter = random.uniform(0, interval * 0.1)
                        self._next_run[key] = now + interval + jitter

                if due:
                    # 按 (tenant_id, domain) 聚合（修正 P0#1）
                    groups = self._group_by_tenant_domain(due)
                    tasks = []
                    for (tenant, domain), targets in groups.items():
                        task = asyncio.create_task(
                            self._run_with_semaphore(tenant, domain, targets)
                        )
                        tasks.append(task)
                    await asyncio.gather(*tasks, return_exceptions=True)

            except Exception as e:
                logger.error("scheduler loop error", err=e)

            await asyncio.sleep(self.settings.scheduler_tick_sec)

    async def _run_with_semaphore(self, tenant_id, domain, targets):
        key = (tenant_id, domain)
        self._in_flight.add(key)
        try:
            async with self._semaphore:
                await asyncio.wait_for(
                    self._execute_round(tenant_id, domain, targets),
                    timeout=self.settings.total_timeout_sec,
                )
        except asyncio.TimeoutError:
            logger.warning("round timeout", tenant=tenant_id, domain=domain)
        finally:
            self._in_flight.discard(key)

    async def _execute_round(self, tenant_id, domain, targets):
        """构建 context 并执行 run_domain。"""
        trace_id = new_trace_id()
        ctx = DetectionContext(
            trace_id=trace_id, tenant_id=tenant_id, domain=domain,
            ...
        )
        # 采集
        await collect(ctx)
        # 运行漏斗
        result = await run_domain(ctx)
        # 更新 detection_state
        await self._update_state(ctx)
        return result

    def _group_by_tenant_domain(self, targets):
        from collections import defaultdict
        groups = defaultdict(list)
        for t in targets:
            groups[(t["tenant_id"], t["domain"])].append(t)
        return groups

# scheduler_lease.py —— 多副本选主
class SchedulerLease:
    """行锁 + TTL 续约 + 崩溃自动接管。"""
    def __init__(self, pool, holder_id: str, ttl_sec: float = 30.0):
        self.pool = pool
        self.holder_id = holder_id
        self.ttl_sec = ttl_sec
        self._acquired = False

    async def try_acquire(self) -> bool:
        """尝试获取 lease。"""
        # INSERT INTO scheduler_lease (holder, acquired_at, expires_at)
        # VALUES (...) ON DUPLICATE KEY UPDATE
        #   holder = IF(expires_at < NOW(), VALUES(holder), holder),
        #   expires_at = IF(holder = VALUES(holder), VALUES(expires_at), expires_at)
        ...

    async def renew(self) -> bool:
        """续约。"""
        # UPDATE scheduler_lease SET expires_at = NOW() + TTL
        # WHERE holder = ? AND expires_at > NOW()
        ...

# reconcile.py —— 修正 P0#9
class Reconciler:
    """独立周期任务：自动关闭已恢复的 problem_record。"""
    def __init__(self, storage, state_store, settings):
        self.storage = storage
        self.state_store = state_store
        self.settings = settings
        self._resolve_after_rounds = settings.resolve_after_rounds  # 连续 N 轮无 anomaly

    async def run(self):
        while self._running:
            await asyncio.sleep(self._resolve_check_interval)
            for tenant_id in await self._get_tenants():
                open_records = await self.storage.records.list(
                    tenant_id, state="pending,in_progress"
                )
                for rec in open_records:
                    # 检查该 group_key 最近 N 轮是否有新 anomaly
                    states = await self.state_store.list_by_domain(
                        tenant_id, rec["domain"]
                    )
                    gk_anomalies = [
                        s for s in states
                        if s["miss_rounds"] >= self._resolve_after_rounds
                    ]
                    if gk_anomalies:
                        await self.storage.records.resolve(
                            tenant_id, rec["record_id"],
                            reason="auto"
                        )

# router/monitors.py
from fastapi import APIRouter, Depends

monitors_router = APIRouter(prefix="/v1/monitors")

@monitors_router.post("")
async def create_monitor(
    body: dict, request: Request,
    tenant_id: str = Depends(get_tenant_id),
    principal: Principal = Depends(get_principal),
):
    # 网关校验
    OutboundGateway.validate_url(body["source_config"]["url"])
    OutboundGateway.validate_headers(body["source_config"].get("headers", {}))
    target_id = await request.app.state.storage.monitor_targets.create(
        tenant_id, body
    )
    return {"target_id": target_id}

@monitors_router.get("")
async def list_monitors(
    request: Request, tenant_id: str = Depends(get_tenant_id),
    service: str | None = None, signal_type: str | None = None,
):
    return await request.app.state.storage.monitor_targets.list(
        tenant_id, service=service, signal_type=signal_type
    )

@monitors_router.post("/{target_id}/run")
async def run_monitor(target_id: str, request: Request):
    """立即执行该端点检测。"""
    tenant_id = request.state.tenant_id
    target = await request.app.state.storage.monitor_targets.get(
        tenant_id, target_id
    )
    if not target:
        raise AppException(ErrorCode.NOT_FOUND, f"monitor {target_id}")
    # 复用 run_round(targets=[t])
    result = await request.app.state.scheduler._execute_round(
        tenant_id, target["domain"], [target]
    )
    return result

# router/problems.py
problems_router = APIRouter(prefix="/v1/problems")

@problems_router.get("")
async def list_problems(
    request: Request, tenant_id: str = Depends(get_tenant_id),
    state: str | None = None, service: str | None = None,
    severity: str | None = None, limit: int = 50,
):
    return await request.app.state.storage.records.list(
        tenant_id, state=state, service=service,
        severity=severity, limit=limit
    )

@problems_router.get("/{record_id}")
async def get_problem(record_id: str, request: Request):
    tenant_id = request.state.tenant_id
    return await request.app.state.storage.records.get(tenant_id, record_id)

# router/plugins.py
plugins_router = APIRouter(prefix="/v1/plugins")

@plugins_router.get("")
async def list_plugins(request: Request):
    return request.app.state.registry.list()

@plugins_router.post("/reload")
async def reload_plugins(request: Request, principal: Principal = Depends(get_principal)):
    if not principal.is_admin:
        raise AppException(ErrorCode.PERMISSION, "admin required")
    request.app.state.registry.reload(...)
    return request.app.state.registry.list()

# router/config.py
config_router = APIRouter(prefix="/v1/config")

@config_router.get("/{domain}")
async def get_config(domain: str, request: Request):
    tenant_id = request.state.tenant_id
    return await request.app.state.config_loader.load(tenant_id)

@config_router.put("/{domain}")
async def update_config(domain: str, body: dict, request: Request,
                        principal: Principal = Depends(get_principal)):
    if not principal.is_admin:
        raise AppException(ErrorCode.PERMISSION, "admin required")
    # 用 M1 模型校验
    validated = DomainConfig(**body)
    await request.app.state.storage.domain_configs.upsert(
        request.state.tenant_id, domain, validated
    )
    # 触发 reload
    await request.app.state.config_loader.reload(request.state.tenant_id)
    return {"status": "ok"}

@config_router.post("/reload")
async def reload_config(request: Request):
    await request.app.state.config_loader.reload(request.state.tenant_id)
    return {"status": "ok"}

# router/maintenance.py
maintenance_router = APIRouter(prefix="/v1/maintenance-windows")

@maintenance_router.post("")
async def create_window(body: dict, request: Request): ...

@maintenance_router.get("")
async def list_windows(request: Request, service: str | None = None): ...

@maintenance_router.delete("/{window_id}")
async def delete_window(window_id: int, request: Request): ...

# router/blacklist.py
blacklist_router = APIRouter(prefix="/v1/blacklist")

@blacklist_router.post("")
async def create_blacklist(body: dict, request: Request): ...

@blacklist_router.get("")
async def list_blacklist(request: Request): ...

@blacklist_router.delete("/{entry_id}")
async def delete_blacklist(entry_id: int, request: Request): ...

# router/alerts.py
alerts_router = APIRouter(prefix="/v1/alerts")

@alerts_router.post("/run")
async def run_all(request: Request, principal: Principal = Depends(get_principal)):
    """手动触发全量检测（调试用）。"""
    if not principal.is_admin:
        raise AppException(ErrorCode.PERMISSION, "admin required")
    # 触发所有到期 + 未到期 target
    ...
```

### Use Case 清单与流程

#### UC-6.1 自动调度检测

```
前置: monitor_target 配置 interval_sec=60；scheduler 已启动并持有 lease
流程:
  1. scheduler_loop 每 tick_sec 检查到期 target
  2. target 到期 → 加入 due 列表
  3. 按 (tenant_id, domain) 聚合
  4. 创建 asyncio.Task（Semaphore 限流）
  5. 每个 task: _execute_round(tenant, domain, targets)
    a. 生成 trace_id
    b. 构建 DetectionContext（包含该 group 所有 target）
    c. collect → L0 → L1 → L2 → L3 → emit（M5 的 run_domain）
    d. 更新 detection_state
  6. 更新 next_run = last_scheduled + interval + jitter
  7. asyncio.wait_for 总超时保护
断言: 自动按 schedule 执行；per-target 不重叠；多 target 同 domain 合并到一个 context
```

#### UC-6.2 手动触发单端点检测

```
前置: 监控端点已创建
流程:
  1. 用户在「监控端点列表」点击某行的「立即执行」
  2. 前端 POST /v1/monitors/{target_id}/run
  3. 后端获取 target 配置
  4. 调用 scheduler._execute_round(tenant, domain, [target])
  5. 返回本轮结果（records, suppressed_count, anomaly_count, timeline）
  6. 前端展示结果摘要
断言: 同步返回结果；不影响自动调度；结果包含 timeline
```

#### UC-6.3 手动触发全量检测

```
前置: admin 用户
流程:
  1. 用户在「触发检测」页点击「全量执行」
  2. 前端 POST /v1/alerts/run
  3. 后端加载所有启用 target
  4. 按 (tenant_id, domain) 聚合
  5. 并发执行所有 round
  6. 收集结果返回
断言: 所有 target 被检测；返回每 domain 的结果摘要
```

#### UC-6.4 问题记录查询

```
前置: 存在 problem_record
流程:
  1. 用户打开「问题列表」页
  2. 前端 GET /v1/problems?state=pending&severity=high&limit=50
  3. RecordStore.list(tenant_id, state=pending, severity=high, limit=50)
  4. 返回匹配的 record 列表
  5. 用户点击某条 → GET /v1/problems/{record_id}
  6. 返回详情（anomalies, correlation, verification, evidence）
断言: 按 tenant_id 隔离；支持 state/severity/service 筛选；详情包含完整信息
```

#### UC-6.5 监控端点 CRUD

```
前置: 用户有对应租户权限
流程:
  1. 创建: POST /v1/monitors → 网关校验 → 生成 target_id → 返回
  2. 列表: GET /v1/monitors → 按 tenant_id 过滤
  3. 修改: PUT /v1/monitors/{id} → 网关校验新配置 → 更新
  4. 删除: DELETE /v1/monitors/{id} → 软删除（enabled=0）
  5. 立即执行: POST /v1/monitors/{id}/run → 复用 run_round
断言: CRUD 按 tenant_id 隔离；source_config 经网关校验；删除后不再调度
```

#### UC-6.6 重新加载检测规则

```
前置: domain_config 表已手动修改
流程:
  1. 用户 POST /v1/config/reload
  2. DomainConfigLoader.reload(tenant_id) → 从 DB 重新加载
  3. 下一轮起使用新配置
断言: 改库即生效；无需重启；旧配置回退机制保留
```

#### UC-6.7 自动关闭（reconcile）

```
前置: 存在 state=pending 的 problem_record；对应 anomaly 连续 N 轮未出现
流程:
  1. Reconciler 定时扫描 open records
  2. 检查 detection_state 中该 group_key 的 miss_rounds
  3. miss_rounds >= resolve_after_rounds（如 3）
  4. RecordStore.resolve(tenant_id, record_id, reason="auto")
  5. record.state → resolved, resolved_at → now, resolve_reason → "auto"
断言: 异常消失后自动关单；resolved_at 记录关闭时间；resolve_reason="auto"
```

#### UC-6.8 多租户鉴权

```
前置: tenant-A 的 API Key 只允许访问 tenant-A
流程:
  1. 请求携带 X-API-Key=tenant-A-key + X-Tenant-Id=tenant-A
  2. AuthMiddleware 校验 API Key → principal(tenants={tenant-A})
  3. X-Tenant-Id=tenant-A ∈ allowed_tenants → 通过
  4. X-Tenant-Id=tenant-B ∉ allowed_tenants → 403
断言: 越权返回 403；所有 store 调用带 tenant_id；无 tenant_id 抛 ValueError
```

#### UC-6.9 多副本选主

```
前置: 两个服务实例同时启动
流程:
  1. 实例 A: SchedulerLease.try_acquire() → 成功（acquired=true）
  2. 实例 B: try_acquire() → 失败（lease 被持有）
  3. 实例 A 每 TTL/2 续约
  4. 实例 A 崩溃 → lease 过期
  5. 实例 B: try_acquire() → 成功（自动接管）
断言: 同一时间只有一个实例跑调度；崩溃后自动接管；不重复开单
```

#### UC-6.10 维护窗口管理

```
前置: 用户有配置权限
流程:
  1. 创建: POST /v1/maintenance-windows {service, start_at, end_at, reason}
  2. 列表: GET /v1/maintenance-windows?service=order-management
  3. 删除: DELETE /v1/maintenance-windows/{id}
  4. 下一轮 L0 自动读取新窗口数据
断言: CRUD 按 tenant_id 隔离；创建后下一轮 L0 自动生效
```

#### UC-6.11 黑名单管理

```
前置: 用户有配置权限
流程:
  1. 创建: POST /v1/blacklist {domain, service, signal, reason, enabled}
  2. 列表: GET /v1/blacklist
  3. 删除: DELETE /v1/blacklist/{id}
  4. 下一轮 L0 自动读取新黑名单
断言: CRUD 按 tenant_id 隔离；创建后下一轮 L0 自动生效
```

---

## M7 — 可观测性、安全加固、交付

### 基础信息

- **目标**：可上线、可排查、可扩展（真第三方插件）
- **依赖**：M6（完整运行），引用 M1 真源
- **关键修正**：P1#15/#16 在此收口；P2 项进入 backlog
- **完成标准**：§13 全 11 用例在 docker-compose 环境通过；Prometheus 指标可被 scrape；压测给出 P99

### 前端菜单与页面

| 菜单路径 | 页面 | 组件 | 说明 |
|---------|------|------|------|
| 监控仪表盘 > 总览 | DashboardPage | `MetricCards` + `RoundTimeline` + `SeverityPieChart` | 展示：轮次成功率、records 创建数、degraded sources、suppressed 总数、FPR、round P99 延迟 |
| 监控仪表盘 > 轮次审计 | RoundAuditPage | `RoundTable` + `TimelineView` | 展示 detection_round：trace_id、domain、timeline、degraded_sources、anomaly_count、record_count |
| 监控仪表盘 > 抑制审计 | SuppressedAuditPage | `SuppressedTable` | 展示 suppressed_detail：signal、suppressor、reason、timestamp |
| 配置管理 > 版本历史 | ConfigHistoryPage | `VersionTimeline` + `ConfigDiff` | 展示 domain_config 版本历史、修改人、JSON diff |
| 系统 > 压测报告 | LoadTestReportPage | `LoadTestChart` | 展示压测结果：QPS、P50/P95/P99 延迟、并发上限建议 |
| 系统 > 安全审计 | SecurityAuditPage | `SecurityLogTable` | 展示安全事件：SSRF 拦截、明文凭据拒绝、越权访问、插件加载失败 |

### 后端实现骨架

```python
# metrics.py
from prometheus_client import Counter, Histogram, Gauge

# 指标定义
round_total = Counter("apm_alert_round_total", "Total detection rounds", ["tenant", "domain"])
round_success = Counter("apm_alert_round_success", "Successful rounds", ["tenant", "domain"])
records_created = Counter("apm_alert_records_created", "Records created", ["tenant", "domain", "severity"])
degraded_sources = Counter("apm_alert_degraded_sources", "Degraded sources", ["tenant", "target_id"])
suppressed_total = Counter("apm_alert_suppressed_total", "Suppressed signals", ["suppressor"])
false_positive_rate = Gauge("apm_alert_false_positive_rate", "FPR by group", ["group_key"])
round_duration = Histogram("apm_alert_round_duration_seconds", "Round duration", ["domain"])

def record_round_metrics(result: DomainResult, tenant_id: str):
    round_total.labels(tenant=tenant_id, domain=result.domain).inc()
    if not result.degraded_sources:
        round_success.labels(tenant=tenant_id, domain=result.domain).inc()
    for r in result.records:
        records_created.labels(tenant=tenant_id, domain=result.domain,
                               severity=r.severity).inc()
    for ds in result.degraded_sources:
        degraded_sources.labels(tenant=tenant_id, target_id=ds).inc()

# audit.py
class AuditLogger:
    """结构化审计日志。"""
    FIELDS = ["trace_id", "tenant_id", "domain", "target_id",
              "signal", "method", "suppressor", "reason", "severity"]

    @staticmethod
    def log_suppressed(ctx, signal, suppressor, reason):
        logger.info("signal_suppressed",
                     trace_id=ctx.trace_id,
                     tenant_id=ctx.tenant_id,
                     domain=ctx.domain,
                     signal=getattr(signal, "metric", getattr(signal, "level", "")),
                     suppressor=suppressor,
                     reason=reason)

    @staticmethod
    def log_round(ctx, result):
        logger.info("detection_round_complete",
                     trace_id=ctx.trace_id,
                     tenant_id=ctx.tenant_id,
                     domain=ctx.domain,
                     anomaly_count=result.anomaly_count,
                     suppressed_count=result.suppressed_count,
                     record_count=len(result.records),
                     degraded_sources=result.degraded_sources,
                     timeline=result.timeline)

# security.py
class SecurityAudit:
    @staticmethod
    def log_ssrf_blocked(url: str, reason: str, trace_id: str):
        logger.warning("ssrf_blocked", url=url, reason=reason, trace_id=trace_id)

    @staticmethod
    def log_plaintext_credential(header_key: str, trace_id: str):
        logger.warning("plaintext_credential_rejected",
                        header=header_key, trace_id=trace_id)

    @staticmethod
    def log_unauthorized(principal_id: str, tenant_id: str, path: str):
        logger.warning("unauthorized_access",
                        principal=principal_id, tenant=tenant_id, path=path)

    @staticmethod
    def log_plugin_load_failed(group: str, name: str, error: str):
        logger.warning("plugin_load_failed", group=group, name=name, error=error)

# router/audit.py
audit_router = APIRouter(prefix="/v1/audit")

@audit_router.get("/rounds")
async def list_rounds(request: Request, tenant_id: str = Depends(get_tenant_id),
                      domain: str | None = None, limit: int = 50):
    """查询 detection_round 审计。"""
    return await request.app.state.storage.round_store.list(
        tenant_id, domain=domain, limit=limit
    )

@audit_router.get("/suppressed")
async def list_suppressed(request: Request, tenant_id: str = Depends(get_tenant_id),
                          domain: str | None = None, limit: int = 50):
    """查询抑制审计。"""
    return await request.app.state.storage.suppressed_store.list(
        tenant_id, domain=domain, limit=limit
    )

# router/metrics.py
from prometheus_client import generate_latest

metrics_router = APIRouter()

@metrics_router.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type="text/plain")

# docker-compose.yml
# version: "3.8"
# services:
#   mysql:
#     image: mysql:8.0
#     environment:
#       MYSQL_ROOT_PASSWORD: test
#     ports: ["3306:3306"]
#   mock-source:
#     build: ./examples/mock_source
#     ports: ["9100:9100"]
#   apm-alert:
#     build: .
#     depends_on: [mysql, mock-source]
#     environment:
#       APM_DB_HOST: mysql
#       APM_DB_PASSWORD: test
#       APM_ENABLE_SCHEDULER: "true"
#     ports: ["8000:8000"]
#   prometheus:
#     image: prom/prometheus
#     ports: ["9090:9090"]
```

### Use Case 清单与流程

#### UC-7.1 Prometheus 指标抓取

```
前置: 服务已启动，/metrics 端点可用
流程:
  1. Prometheus 配置 scrape target: http://apm-alert:8000/metrics
  2. 每 15s 抓取一次
  3. 指标包括: apm_alert_round_total, apm_alert_round_success,
     apm_alert_records_created, apm_alert_degraded_sources,
     apm_alert_suppressed_total, apm_alert_round_duration_seconds
  4. 前端 DashboardPage 从 Prometheus 查询并渲染图表
断言: /metrics 返回 prometheus 格式文本；指标值随检测轮次递增
```

#### UC-7.2 检测轮次审计查询

```
前置: detection_round 表有记录
流程:
  1. 用户打开「轮次审计」页
  2. GET /v1/audit/rounds?domain=application&limit=50
  3. 返回最近 50 轮的: trace_id, domain, timeline, degraded_sources, anomaly_count, record_count
  4. 用户点击某轮 → 展示 timeline 详情
断言: 按 tenant_id 隔离；timeline 包含各阶段时间戳
```

#### UC-7.3 第三方插件验证

```
前置: examples/custom_detector 已 pip install
流程:
  1. 安装: pip install aiops-apm-latency-detector
  2. POST /v1/plugins/reload
  3. GET /v1/plugins → p95_latency 出现在 detector 列表
  4. 在 domain_config 添加 {"plugin": "p95_latency", "params": {"threshold": 500}}
  5. POST /v1/config/reload
  6. 下一轮检测使用 p95_latency 检测器
  7. 延迟 > 500 的信号 → MetricAnomaly
断言: 第三方插件被自动发现；可在 config 引用；检测生效
```

#### UC-7.4 Docker 一键演示

```
前置: docker-compose.yml 已配置
流程:
  1. docker compose up
  2. MySQL 启动 → apm-alert 启动 → mock-source 启动 → Prometheus 启动
  3. apm-alert 自动迁移数据库
  4. seed domain_config + monitor_target
  5. scheduler 开始按 schedule 检测
  6. 前端访问 http://localhost:8000 查看问题列表
  7. Prometheus 查看指标
断言: 一键启动完整演示环境；§13 全 11 用例通过
```

#### UC-7.5 压测

```
前置: 服务已部署
流程:
  1. 运行压测脚本（locust 或 k6）
  2. 模拟 N 个 tenant × M 个 target × 60s interval
  3. 收集: QPS、P50/P95/P99 单轮延迟、records/s、degraded rate
  4. 标定全局并发上限
  5. 生成压测报告
断言: P99 < total_timeout_sec；无 OOM；并发上限有明确建议
```

#### UC-7.6 安全回归测试

```
前置: 服务已启动
流程:
  1. SSRF 回归: 提交 169.254.169.254 / 127.0.0.1 / 10.x → 全部被拒绝
  2. 明文凭据回归: 提交 Bearer abc123 → 被拒绝
  3. 越权回归: tenant-A key 访问 tenant-B → 403
  4. 插件加载失败回归: 安装坏插件 → 不影响其他插件
  5. 配置校验回归: 提交非法 domain_config → ConfigValidationError
断言: 所有安全测试用例通过；安全审计日志有记录
```

---

## 附录 A — 全前端菜单树总览

```
APM 告警管理系统
├── 告警管理
│   ├── 问题列表（ProblemListPage）         [M5] → 增强 [M6]
│   ├── 问题详情（ProblemDetailPage）       [M5] → 增强 [M6]
│   └── 检测状态（DetectionStatePage）       [M5]
│
├── 监控管理
│   ├── 监控端点列表（MonitorListPage）      [M3] → 增强 [M6]
│   ├── 新建端点（MonitorFormPage）          [M3]
│   ├── 端点详情（MonitorDetailPage）        [M3]
│   └── 采集测试（CollectorTestPage）        [M3]
│
├── 配置管理
│   ├── 检测规则（DomainConfigPage）         [M4]
│   ├── 规则版本历史（ConfigVersionPage）    [M4]
│   ├── 维护窗口（MaintenanceWindowPage）   [M6]
│   ├── 黑名单（BlacklistPage）              [M6]
│   └── 误报率（FprPage）                    [M6]
│
├── 系统管理
│   ├── 插件管理（PluginListPage）           [M4]
│   ├── 调度状态（SchedulerStatusPage）      [M6]
│   ├── 数据库迁移（MigrationPage）         [M2]
│   ├── 数据库状态（DBStatusPage）           [M2]
│   └── 安全审计（SecurityAuditPage）        [M7]
│
├── 监控仪表盘
│   ├── 总览（DashboardPage）                [M7]
│   ├── 轮次审计（RoundAuditPage）           [M7]
│   └── 抑制审计（SuppressedAuditPage）      [M7]
│
├── 手动操作
│   └── 触发检测（TriggerPage）              [M6]
│
└── 系统
    ├── 健康状态（HealthPage）               [M0]
    └── 错误页（ErrorPage）                  [M0]
```

## 附录 B — 全 Use Case 索引（57 条）

| 阶段 | UC 编号 | 名称 | 对应 §13 |
|------|---------|------|---------|
| M0 | UC-0.1 | 系统启动健康检查 | — |
| M0 | UC-0.2 | 环境变量覆盖配置 | — |
| M0 | UC-0.3 | 异常标准化响应 | — |
| M1 | UC-1.1 | Signal 序列化与判别器 | — |
| M1 | UC-1.2 | Anomaly 指纹稳定性 | — |
| M1 | UC-1.3 | Group Key 排序无关性 | — |
| M1 | UC-1.4 | 插件契约校验 | — |
| M2 | UC-2.1 | 数据库迁移执行 | — |
| M2 | UC-2.2 | 新开 problem_record | — |
| M2 | UC-2.3 | 追加 evidence（去重命中）| — |
| M2 | UC-2.4 | 已关闭记录复发开单 | — |
| M2 | UC-2.5 | 配置加载与 Seed | — |
| M2 | UC-2.6 | 配置加载失败回退 | — |
| M3 | UC-3.1 | 新增监控端点 | — |
| M3 | UC-3.2 | 测试采集连通性 | — |
| M3 | UC-3.3 | 指标采集（Prometheus） | — |
| M3 | UC-3.4 | 日志采集（HTTP） | — |
| M3 | UC-3.5 | 水位线推进与幂等去重 | 用例 3 |
| M3 | UC-3.6 | 采集源超时降级 | 用例 10 |
| M3 | UC-3.7 | SSRF 拦截 | — |
| M3 | UC-3.8 | Secret 引用解析 | — |
| M4 | UC-4.1 | 查看已加载插件列表 | — |
| M4 | UC-4.2 | 重新加载插件 | — |
| M4 | UC-4.3 | 配置静态阈值检测器 | — |
| M4 | UC-4.4 | 配置环比检测器 | — |
| M4 | UC-4.5 | 配置签名聚合检测器 | 用例 3 |
| M4 | UC-4.6 | 配置维护窗口抑制器 | 用例 7 |
| M4 | UC-4.7 | 配置黑名单抑制器 | — |
| M4 | UC-4.8 | 第三方插件安装与发现 | — |
| M5 | UC-5.1 | CPU 飙高两轮 | 用例 1 |
| M5 | UC-5.2 | 内存泄漏 + Full GC 组合 | 用例 2 |
| M5 | UC-5.3 | 47 条 OOM 日志聚合 | 用例 3 |
| M5 | UC-5.4 | 指标+日志同源关联 | 用例 4 |
| M5 | UC-5.5 | 错误率突增 + 部署变更 | 用例 5 |
| M5 | UC-5.6 | 瞬时抖动过滤 | 用例 6 |
| M5 | UC-5.7 | 维护窗口抑制 | 用例 7 |
| M5 | UC-5.8 | 误报率闸门 | 用例 8 |
| M5 | UC-5.9 | 无信号提前终止 | 用例 9 |
| M5 | UC-5.10 | 日志源超时降级 | 用例 10 |
| M5 | UC-5.11 | 单条 info 弱信号不升级 | 用例 11 |
| M6 | UC-6.1 | 自动调度检测 | — |
| M6 | UC-6.2 | 手动触发单端点检测 | — |
| M6 | UC-6.3 | 手动触发全量检测 | — |
| M6 | UC-6.4 | 问题记录查询 | — |
| M6 | UC-6.5 | 监控端点 CRUD | — |
| M6 | UC-6.6 | 重新加载检测规则 | — |
| M6 | UC-6.7 | 自动关闭（reconcile） | — |
| M6 | UC-6.8 | 多租户鉴权 | — |
| M6 | UC-6.9 | 多副本选主 | — |
| M6 | UC-6.10 | 维护窗口管理 | — |
| M6 | UC-6.11 | 黑名单管理 | — |
| M7 | UC-7.1 | Prometheus 指标抓取 | — |
| M7 | UC-7.2 | 检测轮次审计查询 | — |
| M7 | UC-7.3 | 第三方插件验证 | — |
| M7 | UC-7.4 | Docker 一键演示 | — |
| M7 | UC-7.5 | 压测 | — |
| M7 | UC-7.6 | 安全回归测试 | — |

## 附录 C — 每阶段 API 端点增量

| 阶段 | 新增端点 | 说明 |
|------|---------|------|
| M0 | `GET /health`, `GET /ready` | 探针 |
| M2 | `POST /v1/migrate`, `GET /v1/migration-status` | 迁移管理 |
| M3 | `POST /v1/monitors`, `GET /v1/monitors`, `PUT /v1/monitors/{id}`, `DELETE /v1/monitors/{id}` | 端点 CRUD |
| M4 | `GET /v1/plugins`, `POST /v1/plugins/reload` | 插件管理 |
| M4 | `GET /v1/config/{domain}`, `PUT /v1/config/{domain}`, `POST /v1/config/reload` | 规则管理 |
| M5 | `GET /v1/problems`, `GET /v1/problems/{id}` | 问题查询 |
| M5 | `GET /v1/detection-state` | 检测状态查看 |
| M6 | `POST /v1/monitors/{id}/run`, `POST /v1/alerts/run` | 手动触发 |
| M6 | `POST/GET/DELETE /v1/maintenance-windows` | 维护窗口 CRUD |
| M6 | `POST/GET/DELETE /v1/blacklist` | 黑名单 CRUD |
| M6 | `GET /v1/scheduler/status` | 调度状态 |
| M7 | `GET /metrics` | Prometheus |
| M7 | `GET /v1/audit/rounds`, `GET /v1/audit/suppressed` | 审计查询 |
