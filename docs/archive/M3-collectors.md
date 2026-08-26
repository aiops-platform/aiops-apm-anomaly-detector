# M3 采集层与出站网关 — 历史规格归档

> 本文归档 `docs/apm-alert-module-design.md`（§6.2 collector 分派、§8.1 监控端点）与 `docs/apm-alert-implementation-plan-enhanced.md`（M3 小节）中**已实现**的部分。实现日志见 [`docs/logs/M3.md`](../logs/M3.md)，实现计划见 [`docs/plans/M3-implementation-plan.md`](../plans/M3-implementation-plan.md)。

## 目标

流程最上游的「数据供给」环节：两个内置 collector（`http_metrics` / `http_logs`）+ `mock` 跑通真实第三方 API，所有出站 HTTP 通过**安全网关**（SSRF 拦截 + secret 引用解析）。M3 同时交付 `monitor_target` 端点管理 API（CRUD + 连通性测试），消费 M2 已建好的 `monitor_target` / `signal_snapshot` / `collect_watermark` 表。

## 数据流定位

```
POST /v1/monitors ──validate_url/validate_headers──> MonitorTargetStore.create → monitor_target
        │
        ▼  (调度器 M6 或 /test 端点触发)
Collector.collect(ctx, target)
  1. gateway.validate_url / validate_headers
  2. resolve_secret  ${env:X} / ${vault:...}
  3. watermark_store.get → 下推 params["start"]=last_ts
  4. SharedHttpClient.request(method, url, headers, params)
  5. FieldMapper.map_metric / map_log → Signal 列表
  6. 幂等去重（metric: hash(metric|value|timestamp)；log: hash(service|signature|timestamp)）
  7. watermark_store.update(last_ts=max(timestamp))   # 水位线推进
  8. snapshot_store.write → signal_snapshot
```

## 交付（8 个 Use Case）

| UC | 名称 | 断言 |
|----|------|------|
| UC-3.1 | 新增监控端点 | monitor_target 表新增一行；url 通过网关校验；target_id 唯一（MT-NNNN，租户内递增） |
| UC-3.2 | 测试采集连通性 | 成功返回信号样本；失败返回结构化错误（SSRF/超时/字段缺失） |
| UC-3.3 | 指标采集（Prometheus API） | 信号数=去重后条目数；watermark 推进；snapshot 写入 |
| UC-3.4 | 日志采集（HTTP API） | 日志信号数=去重后条数；每条 LogSignal 携带 signature 预计算值 |
| UC-3.5 | 水位线推进与幂等去重 | 第二轮 signals 为空；watermark 未回退 |
| UC-3.6 | 采集源超时降级 | 服务不崩溃；collector 抛错被调用方捕获；其余 source 正常 |
| UC-3.7 | SSRF 拦截 | 127.0.0.1 / 10.x / 192.168.x / 169.254.x / ::1 被拒；表无新增 |
| UC-3.8 | Secret 引用解析 | ${env:X} 可解析；明文凭据被拒；env 不存在返回空串 |

## collector 分派矩阵（设计 §6.2，M3 落地 `collector_for`）

| signal_type | source_type | collector 插件 |
|-------------|-------------|----------------|
| log | http / elk | `http_logs` |
| metric | prometheus / http | `http_metrics` |
| * | mock | `mock` |

M3 用**直接 import** 分派（`collectors/__init__.py::collector_for`）；`entry_points` 声明已放开，M4 插件 registry 再消费。

## 监控端点配置（设计 §8.1，M3 落地 CRUD API）

`monitor_target` 表一行（service=order-management，日志监控）：

```jsonc
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

- **`source_config`**：完全由 collector 插件解释。`http_logs`/`http_metrics` 识别 `url/method/headers/params/field_mapping`（另支持 `rows_path` 抽取数组路径，Prometheus 默认 `data.result`）；`field_mapping` 把第三方响应字段映射到 `LogSignal`/`MetricSignal`，支持点路径（`metric.__name__`）与数组索引（`value[1]`）。
- **自服务入口**（M3 已实现）：`POST /v1/monitors` 新增、`GET /v1/monitors` 列表（service/signal_type 过滤）、`GET/PUT/DELETE /v1/monitors/{target_id}` 详情/更新/软删、`POST /v1/monitors/{target_id}/test` 连通性测试。`POST /v1/monitors/{target_id}/run` 立即执行随 M6（调度器）。

## 出站安全网关（P0#6 SSRF / secret，M3 落地）

- `validate_url`：scheme 白名单 `{http, https}`；hostname 解析为 IP 字面量且在 `BLOCKED_NETWORKS`（127/8、10/8、172.16/12、192.168/16、169.254/16、::1/128）→ 拒；域名跳过（DNS 解析后二次校验留 M7 加固）。
- `validate_headers`：明文凭据（`Bearer xxx` / `AKIA...` / `ghp_...` / `sk-...`）拒绝；`authorization` / `x-api-key` 的值必须含 `${env:X}` / `${vault:path#key}` 引用。
- `resolve_secret`：`${env:X}` → `os.environ.get(X, "")`（缺失空串）；`${vault:...}` 为占位（M7 接密钥管理系统）。

## 关键实现

- **迁移**：`V2__collect_watermark.sql` — `collect_watermark` 表（`PRIMARY KEY (tenant_id, target_id)`、`last_ts DATETIME(3)`），`make migrate` 幂等应用。
- **存储**：`MonitorTargetStore`（create 生成 `MT-%04d`、list/get/update/软删 delete/load_all_targets）、`SnapshotStore`（write 把 MetricSignal/LogSignal 分派为 signal_snapshot 行）、`WatermarkStore`（get/update，`INSERT ... ON DUPLICATE KEY UPDATE`）；每 store 三件套 ABC + InMemory + MySQL，`if not tenant_id: raise ValueError`。
- **signature**：`signature(log, n_frames=3)` 纯函数（exc 首行 + 前 n 个 frame 去括号拼接；无 stack_trace 回退 `message[:120]`）；`LogSignal` 增可选字段 `signature`（M1 契约允许）。
- **collectors**：`_gateway.py`（OutboundGateway）、`_http_client.py`（SharedHttpClient，httpx 超时/连接池/禁止跳转 + 响应大小限制）、`_field_mapping.py`（FieldMapper）、`_context.py`（CollectContext）、`http_metrics.py` / `http_logs.py` / `mock.py`。
- **API**：`router/deps.py::get_tenant_id`（`X-Tenant-Id` 头，默认 `default`，服务端解析不信任 body）、`router/monitors.py` `/v1/monitors`。

## 范围（不做，留后续里程碑）

- **通知/告警出站网关**（webhook/slack）— M5 emit 范畴。
- **并行调度 `asyncio.gather` + `degraded_sources`** — M6 collect() 职责；M3 只保证 collector 超时抛错、调用方捕获不崩溃。
- **DNS 二次校验** — M7 安全加固。
- **插件 registry / entry_points 动态发现** — M4 落地；M3 直接 import 分派。
- **前端页面**（MonitorListPage 等）— M0 明确不做前端。
