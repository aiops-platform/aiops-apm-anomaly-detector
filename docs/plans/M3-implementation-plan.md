# M3 采集层与出站网关 — 实现计划

> 状态：**已实现**（2026-08-26）。实现日志见 `docs/logs/M3.md`，历史规格归档见 `docs/archive/M3-collectors.md`。

## Context（为什么做）

- **前置**：M0 工程基座 + M1 契约层 + M2 持久化与迁移已完成（`make lint test dev` 全绿，45 用例）。M1+M2 已按里程碑提交（M1=`feb09ff`，M2=`3d7f121`）。
- **M3 是什么**：流程最上游的「数据供给」环节。两个内置 collector（`http_metrics` / `http_logs`）+ `mock` 跑通真实第三方 API，且所有出站 HTTP 通过**安全网关**（SSRF 拦截 + secret 引用解析）。M3 同时交付 `monitor_target` 端点管理 API（CRUD + 连通性测试）。
- **整体位置**：`监控端点配置 + 数据采集 → L0–L3 漏斗 → emit problem_record`。M3 是数据来源；M4 消费 `signal_snapshot` 做检测；M2 已建好的 `monitor_target`/`signal_snapshot`/`change_record` 三张表由 M3 消费（change_record 由 CI/CD 写入，M3 不产 ChangeSignal）。
- **完成标准**（设计文档原文）：连续两轮采集同一稳定源，第二轮返回 0 新信号（水位线生效）；SSRF 测试用例被网关拒绝。
- **用户已确认的决策**：
  1. git：先按里程碑分别提交 M1、M2，再实现 M3，最后 M3 单独提交。
  2. M3 出站网关是**安全网关**（SSRF + secret 引用），**不是**通知网关（通知渠道无设计文档，属 M5 emit 范畴）。
  3. 范围：**完整 M3**（采集器 + 安全网关 + 端点管理 API + 水位线/快照 store + V2 迁移 + 测试文档）。

## 改动点：位置与用途

```
src/aiops_apm/
├── migrations/V2__collect_watermark.sql      # 新增：collect_watermark 表（M2 遗留）
├── storage/
│   ├── monitor_targets.py                    # 新增：MonitorTargetStore ABC + InMemory + MySQL
│   ├── snapshots.py                          # 新增：SnapshotStore（写 signal_snapshot）
│   ├── watermarks.py                         # 新增：WatermarkStore（collect_watermark）
│   └── __init__.py                           # 改：Storage 聚合 + build_storage 分派
├── signature.py                              # 新增：signature() 纯函数（L1 日志聚合共享）
├── models/signal.py                          # 改：LogSignal 加可选字段 signature
├── collectors/
│   ├── __init__.py                           # 新增：collector_for() 分派
│   ├── _gateway.py                           # 新增：OutboundGateway（SSRF + secret）
│   ├── _http_client.py                       # 新增：SharedHttpClient（httpx 封装）
│   ├── _field_mapping.py                     # 新增：FieldMapper（响应→Signal）
│   ├── _context.py                           # 新增：CollectContext（M3 最小 ctx 占位）
│   ├── http_metrics.py                       # 新增：HttpMetricsCollector + build()
│   ├── http_logs.py                          # 新增：HttpLogsCollector + build()
│   └── mock.py                               # 新增：MockCollector + build()
├── router/
│   ├── deps.py                               # 新增：get_tenant_id（X-Tenant-Id）
│   └── monitors.py                           # 新增：/v1/monitors CRUD + /test
└── _app.py                                   # 改：lifespan 建 http_client；挂 monitors router
pyproject.toml                                # 改：httpx → 运行时依赖；放开 collectors entry_points
tests/                                        # 新增 6 个测试文件 + 扩展 test_migrations.py
docs/plans/M3-implementation-plan.md          # 本文档（状态 进行中 → 已实现）
docs/logs/M3.md                               # 实现日志
docs/archive/M3-collectors.md                 # 归档已实现章节
README.md / CLAUDE.md                         # 进度同步
```

**数据流**：

```
POST /v1/monitors ──OutboundGateway.validate_url/validate_headers──> MonitorTargetStore.create → monitor_target
        │
        ▼  (调度器 M6 或 /test 端点触发)
Collector.collect(ctx, target)
  1. gateway.validate_url(url) / validate_headers(headers)
  2. resolve_secret(headers)  ${env:X} / ${vault:...}
  3. watermark_store.get(tenant, target_id) → 下推 params["start"]=last_ts
  4. SharedHttpClient.request(method, url, headers, params)
  5. FieldMapper.map_metric/map_log → Signal 列表
  6. 幂等去重（metric|value|timestamp hash）
  7. watermark_store.update(last_ts=max(timestamp))   # 水位线推进
  8. snapshot_store.write → signal_snapshot
```

## 范围

### 交付（8 个 Use Case）

| UC | 名称 | 断言 |
|----|------|------|
| UC-3.1 | 新增监控端点 | monitor_target 表新增一行；url 通过网关校验；target_id 唯一（MT-NNNN） |
| UC-3.2 | 测试采集连通性 | 成功返回信号样本；失败返回结构化错误（SSRF/超时/字段缺失） |
| UC-3.3 | 指标采集（Prometheus API） | 信号数=去重后条目数；watermark 推进；snapshot 写入 |
| UC-3.4 | 日志采集（HTTP API） | 日志信号数=去重后条数；每条 LogSignal 携带 signature 预计算值 |
| UC-3.5 | 水位线推进与幂等去重 | 第二轮 signals 为空；watermark 未回退 |
| UC-3.6 | 采集源超时降级 | 服务不崩溃；collector 抛错被调用方捕获；其余 source 正常 |
| UC-3.7 | SSRF 拦截 | 127.0.0.1 / 10.x / 192.168.x / 169.254.x / ::1 被拒；表无新增 |
| UC-3.8 | Secret 引用解析 | ${env:X} 可解析；明文凭据被拒；env 不存在返回空串 |

### 不做（明确排除）
- **通知/告警出站网关**（webhook/slack 等）——无设计文档，M5 emit 范畴。
- **调度器 / poller / collect() 并行调度**——M6 落地（`asyncio.gather` + `degraded_sources` 是 M6 collect() 的职责）；M3 只保证 collector 超时抛错、调用方（/test 端点、单测）能捕获不崩溃。
- **DNS 二次校验**（validate_url 只拦 IP 字面量；域名解析后重查留 M7 安全加固）。
- **插件 registry / entry_points 动态发现**——M4 落地；M3 用**直接 import** 分派 collector。
- **change_record store / ChangeSignal 采集**——由 CI/CD 写，见设计 §14 open item #5。
- **前端页面**（MonitorListPage 等）——M0 明确不做前端；仅后端 API。

## 文件清单（新增）

```
src/aiops_apm/migrations/V2__collect_watermark.sql
src/aiops_apm/storage/monitor_targets.py
src/aiops_apm/storage/snapshots.py
src/aiops_apm/storage/watermarks.py
src/aiops_apm/signature.py
src/aiops_apm/collectors/__init__.py
src/aiops_apm/collectors/_gateway.py
src/aiops_apm/collectors/_http_client.py
src/aiops_apm/collectors/_field_mapping.py
src/aiops_apm/collectors/_context.py
src/aiops_apm/collectors/http_metrics.py
src/aiops_apm/collectors/http_logs.py
src/aiops_apm/collectors/mock.py
src/aiops_apm/router/deps.py
src/aiops_apm/router/monitors.py
tests/test_gateway.py
tests/test_signature.py
tests/test_field_mapping.py
tests/test_collectors.py
tests/test_monitor_targets.py
tests/test_snapshots.py
tests/test_watermarks.py
tests/test_monitors_api.py
```

## 关键实现细节

### 1. 迁移：`migrations/V2__collect_watermark.sql`
```sql
CREATE TABLE IF NOT EXISTS collect_watermark (
    tenant_id   VARCHAR(64) NOT NULL DEFAULT 'default',
    target_id   VARCHAR(32) NOT NULL,
    last_ts     DATETIME(3) NOT NULL,
    updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (tenant_id, target_id)
) ENGINE=InnoDB;
```
MigrationRunner 自动发现 `V*__*.sql`，`make migrate` 幂等应用。

### 2. Stores（模式照抄 `storage/domain_config.py` / `storage/records.py`）
每个 ABC 三件套：`XxxStore(ABC)` + `InMemoryXxxStore` + `MySQLXxxStore(pool: ConnectionPool)`；每个方法入口校验 `if not tenant_id: raise ValueError(...)`；JSON 列用 `_as_json`/`_decode_json`。

**MonitorTargetStore**（行：`{target_id, service, signal_type, source_type, domain, source_config(dict), schedule(dict), enabled}`）：
- `create(tenant_id, target: dict) -> dict` — 生成 `target_id = MT-%04d`（查该租户现有最大后缀 +1），INSERT 返回行。
- `list(tenant_id, *, service=None, signal_type=None) -> list[dict]`
- `get(tenant_id, target_id) -> dict | None`
- `update(tenant_id, target_id, patch: dict) -> dict | None`
- `delete(tenant_id, target_id) -> None`（软删 `enabled=0`）
- `load_all_targets(tenant_id) -> list[dict]`（enabled，M6 调度器用）

**SnapshotStore**：
- `write(tenant_id, target_id, signals: list) -> int` — 每条 signal 一行写入 `signal_snapshot`（snapshot_ts=now；MetricSignal→metric/value/labels；LogSignal→level/message/signature）。memory 版追加到 `_rows`。

**WatermarkStore**：
- `get(tenant_id, target_id) -> dict | None`（`{"last_ts": datetime}`）
- `update(tenant_id, target_id, last_ts: datetime) -> None`（INSERT ... ON DUPLICATE KEY UPDATE last_ts=VALUES(last_ts)）

**`storage/__init__.py`**：`Storage` 加 `monitor_targets`、`snapshots`、`watermarks` 三字段；`build_storage` memory→InMemory 三件套，mysql→MySQL 三件套。

### 3. `signature.py`（纯函数，M4 signature_aggregate 共享）
```python
def signature(log: LogSignal, n_frames: int = 3) -> str:
    if not log.stack_trace:
        return log.message[:120]
    lines = log.stack_trace.strip().split("\n")
    exc = lines[0].split(":")[0] if lines else log.message
    frames = [ln.strip().split("(")[0] for ln in lines[1:1 + n_frames]]
    return "|".join([exc, *frames])
```
`models/signal.py`：LogSignal 加**可选字段** `signature: str | None = None`（M1 契约允许只加可选字段；与 signal_snapshot.signature / LogAnomaly.signature 对齐）。

### 4. `collectors/_gateway.py` — OutboundGateway（设计原文）
```python
class OutboundGateway:
    ALLOWED_SCHEMES = {"http", "https"}
    BLOCKED_NETWORKS = [ip_network("127.0.0.0/8"), ip_network("10.0.0.0/8"),
                        ip_network("172.16.0.0/12"), ip_network("192.168.0.0/16"),
                        ip_network("169.254.0.0/16"), ip_network("::1/128")]
    SECRET_REF_PATTERN = re.compile(r"^\$\{(env|vault):.+\}$")
    PLAINTEXT_CRED_PATTERN = re.compile(
        r"(Bearer\s+[A-Za-z0-9\-_\.]+|AKIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,})", re.IGNORECASE)

    @classmethod
    def validate_url(cls, url) -> str:
        # scheme 不在白名单 → AppException(VALIDATION, ...)
        # hostname 解析为 IP 且在 BLOCKED_NETWORKS → AppException(VALIDATION, "blocked network: {ip}")
        # ValueError → 域名，跳过（DNS 二次校验留 M7）
    @classmethod
    def validate_headers(cls, headers) -> dict:
        # 明文凭据命中 → AppException；authorization/x-api-key 必须匹配 SECRET_REF_PATTERN
    @classmethod
    def resolve_secret(cls, ref) -> str:
        # "${env:X}" → os.environ.get(X, "")；"${vault:path#key}" → 占位返回 ""
```
错误码用已有 `ErrorCode.VALIDATION`（exceptions.py 已含）。

### 5. `collectors/_http_client.py` — SharedHttpClient
```python
class SharedHttpClient:
    def __init__(self, settings: Settings, *, transport=None):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.outbound_timeout_sec),
            limits=httpx.Limits(max_connections=50),
            follow_redirects=False,
            transport=transport,          # 测试注入 MockTransport
        )
    async def request(self, method, url, **kwargs) -> httpx.Response: ...
    async def aclose(self) -> None: ...
```
> 注意：httpx `AsyncClient` **没有** `max_content_length` 参数（设计骨架里写错了）。大小限制改为 `request()` 返回后检查 `len(resp.content) > settings.outbound_max_body_bytes` → 抛 `AppException(VALIDATION, "response too large")`。实现时确认。

### 6. `collectors/_field_mapping.py` — FieldMapper
- `_extract_path(row, spec)`: 支持 `"message"`、`"metric.__name__"`（点路径）、`"value[1]"`（数组索引，Prometheus `value` 形如 `[ts, "0.91"]`）。
- `_parse_ts(value)`: 支持 ISO 字符串 与 unix 秒（float/int）→ datetime。
- `map_metric(row, mapping, tenant_id) -> MetricSignal`
- `map_log(row, mapping, tenant_id) -> LogSignal`

### 7. `collectors/_context.py` — CollectContext（M3 最小 ctx，M5 DetectionContext 取代）
```python
@dataclass
class CollectContext:
    tenant_id: str
    watermark_store: WatermarkStore | None = None
    snapshot_store: SnapshotStore | None = None
```
Collector 在 `ctx.watermark_store is not None` 时才读写水位线/快照 → 支持 /test 连通性模式（不写库）。

### 8. `collectors/http_metrics.py` / `http_logs.py` / `mock.py`
照设计骨架实现 `collect(ctx, target)`：validate→resolve→watermark 下推→request→map→hash 去重→watermark update→snapshot write→返回 signals。`build(*, http=None, pool=None, settings=None) -> Collector` 返回 `HttpMetricsCollector(http, OutboundGateway())`。
- `http_logs` 额外：每条 LogSignal 设 `signature=signature(log_signal, n_frames=3)`；去重 hash 用 `(service, signature, timestamp)`。
- `mock`：返回 `target.get("_mock_signals", [])`。
- `collectors/__init__.py` 提供 `collector_for(target, *, http=None, settings=None) -> Collector`：`source_type=="mock"`→mock；`log + (http|elk)`→http_logs；`metric + (http|prometheus)`→http_metrics；否则 `AppException(CONFIG_ERROR, ...)`。

### 9. Router
- `router/deps.py`：`get_tenant_id(request) -> str`（`X-Tenant-Id` 头，默认 `"default"`；服务端解析，绝不信任 body）。
- `router/monitors.py`（`prefix="/v1/monitors"`，挂到 api_router）：
  - `POST /` — 建端点：先 `OutboundGateway.validate_url` + `validate_headers`（UC-3.1/3.7/3.8），再 `MonitorTargetStore.create`，201 `{"target_id": "MT-0001"}`。
  - `GET /` — 列表（`service`/`signal_type` query filter）。
  - `GET /{target_id}` — 详情，404 if None。
  - `PUT /{target_id}` — 更新（若 url/headers 变更则重新网关校验）。
  - `DELETE /{target_id}` — 软删，204。
  - `POST /{target_id}/test` — 连通性测试（UC-3.2）：取 target→`collector_for`→`CollectContext(tenant_id)`（watermark/snapshot=None）→一次 collect（不写库）→返回 `{"target_id", "status":"ok"|"error", "signal_count", "signals": 前20个样本}`；网关校验错误走 AppException→4xx，上游失败返回 `{"status":"error","reason":...}`（200）。
- `_app.py` lifespan：`app.state.http_client = SharedHttpClient(settings)`，finally `aclose()`；`api.py` include `monitors` router。

### 10. `pyproject.toml`
- `httpx>=0.27` 从 dev 移到 `dependencies`（SharedHttpClient 运行时依赖）。
- 取消注释 `[project.entry-points."aiops_apm.collectors"]` 三行（M4 的 registry 消费；M3 用直接 import 分派）。

## 测试（TDD，先写测试再实现）

| 测试文件 | UC | 断言 |
|---------|----|------|
| `test_gateway.py` | 3.7/3.8 | validate_url scheme/私网拒绝、域名放行；validate_headers 明文拒/secret 引用要求；resolve_secret env 解析/vault 占位/缺失空串 |
| `test_signature.py` | 3.4 | 有/无 stack_trace 的 signature 形状；n_frames 截断；message[:120] 回退 |
| `test_field_mapping.py` | 3.3/3.4 | map_metric/map_log；`value[1]` 抽取；`metric.__name__` 点路径；_parse_ts ISO/unix |
| `test_collectors.py` | 3.3/3.4/3.5/3.6 | FakeHttp（`request()`）注入；水位线下推 params；hash 去重；watermark 推进；第二轮空；超时抛错被捕获；snapshot 写入；mock collector |
| `test_monitor_targets.py` | 3.1 | create/list/get/update/delete；target_id 唯一递增；tenant 隔离；软删；缺 tenant 抛 ValueError |
| `test_snapshots.py` | 3.3/3.4 | InMemory snapshot 写入 metric/log 行（含 signature 列） |
| `test_watermarks.py` | 3.5 | get/update；重复 update 覆盖不回退 |
| `test_monitors_api.py` | 3.1/3.2/3.7/3.8 | TestClient + memory backend：POST/GET/PUT/DELETE；X-Tenant-Id 生效；SSRF URL 建端点→400；/test 成功与失败 |
| `test_migrations.py`（扩展） | 2.1 | 断言 V2 含 `collect_watermark` 表、主键 `(tenant_id, target_id)` |

内存实现是单测真源（对齐 `test_records.py` 用 InMemoryRecordStore 的模式）；测试用 `Settings(_env_file=None, storage_backend="memory")`。

## 验证（完成标准）

1. `make lint` — ruff + mypy 全绿（新代码含 `# type: ignore[import-untyped]` 处理 httpx/aiomysql 无 stub 处）。
2. `make test` — 全量 pytest 绿（原 45 + M3 新增用例）。
3. `make migrate` — 真库幂等应用 V2，`collect_watermark` 表建立（本机无 mysql CLI，用 `ConnectionPool.fetchall("SHOW TABLES")` 验证，见 memory）。
4. 手动 API 冒烟（`make dev` + curl）：
   - `POST /v1/monitors` 建 Prometheus 端点 → 201 `{"target_id":"MT-0001"}`；SSRF URL `http://169.254.169.254/...` → 400。
   - `POST /v1/monitors/{target_id}/test` → 成功返回信号样本 / 失败结构化错误。
5. 完成标准复核：连续两轮 collect 同一稳定源，第二轮 `signals==[]`（单测 UC-3.5 覆盖）。

## 文档同步（CLAUDE.md 流程）

1. 本文档（`docs/plans/M3-implementation-plan.md`）落库，状态「进行中」。
2. 完成后写 `docs/logs/M3.md` 实现日志（改动点、文件清单、完成状态、遗留问题）。
3. 归档已实现章节到 `docs/archive/M3-collectors.md`；从设计文档摘除 M3 已实现部分。
4. 更新 `README.md` 进度表（新增 collectors/stores/API 用法）。
5. 更新 `CLAUDE.md`「当前里程碑」为「M3 已完成，下一阶段 M4 检测层」；本文档状态改「已实现」。
6. 最后提交 M3（feat）。
