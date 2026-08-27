# aiops-apm-anomaly-detector 端到端操作手册（手动跑通指南）

> 目标：让一名运维 / 开发读完本手册后，能独立完成「配置 → 启动 → 采集 → 检测 → 落库 → 查询/审计」全链路跑通，
> 并亲眼看到一条 `problem_record`。文档覆盖：整体流程图、需要哪些「页面」、如何配置、一步一步手动配置、端到端验证清单。
>
> 适用范围：M0–M7 已实现能力（`make lint test dev` 全绿，351 用例）。实现事实来源见
> [`apm-alert-module-design.md`](apm-alert-module-design.md) 与 [`apm-alert-implementation-plan-enhanced.md`](apm-alert-implementation-plan-enhanced.md)。

---

## 目录

1. [整体流程图](#1-整体流程图)
2. [需要哪些「页面」](#2-需要哪些页面)
3. [如何配置](#3-如何配置)
4. [一步一步手动配置与跑通](#4-一步一步手动配置与跑通)
5. [端到端验证清单](#5-端到端验证清单checklist)
6. [已知边界与常见问题](#6-已知边界与常见问题)
7. [完整命令速查](#7-完整命令速查)

---

## 1. 整体流程图

### 1.1 系统全景（配置面 → 运行面 → 输出面）

```
┌──────────────────────── 配置面（写路径：人/平台 → 数据库）────────────────────────┐
│                                                                                   │
│  环境变量 .env / APM_*           → Settings（服务/DB/调度/鉴权/出站）              │
│  POST /v1/monitors               → monitor_target   （监控谁、从哪采、多快采）      │
│  PUT  /v1/config/{domain}        → domain_config    （怎么判、怎么抑制、怎么验证）   │
│  POST /v1/maintenance-windows    → maintenance_window（维护窗口）                  │
│  POST /v1/blacklist              → suppress_blacklist（黑名单）                    │
│  entry_points（包安装）          → 插件 registry（collector / detector / suppressor）│
│  POST /v1/plugins/reload         → 重扫 entry_points（原子热替换，无需重启）        │
└───────────────────────────────────────────────────────────────────────────────────┘
                                  │  每轮检测读取（build_context）
                                  ▼
┌──────────────────────── 运行面（读路径：每轮检测 / 恢复闭环）──────────────────────┐
│                                                                                   │
│  Scheduler  每 tick：lease 门（多副本只一个调度）→ 找 due target → 限流 → run_round │
│  手动触发    POST /v1/alerts/run            （全量 / ?domain= 过滤）                │
│              POST /v1/monitors/{id}/run     （单端点）                             │
│  Poller      run_round：按 (tenant, domain) 分组并行 collect → run_domain           │
│  Reconciler  周期扫 pending 单 → 各 anomaly_key 连续 miss 达标 → resolve(auto) 关单  │
│                                                                                   │
└───────────────────────────────────────────────────────────────────────────────────┘
                                  │  写库
                                  ▼
┌──────────────────────── 输出面（查询 / 观测）─────────────────────────────────────┐
│                                                                                   │
│  problem_record   → GET /v1/problems            问题单（pending/resolved）         │
│  detection_round  → GET /v1/audit/rounds        轮次审计（success/partial/failed） │
│  signal_snapshot / detection_state / collect_watermark（运行期中间态）             │
│  /metrics         → Prometheus 7 类指标（round/success、records、degraded、        │
│                      suppressed、false_positive_rate、round_duration）             │
│  fpr_table        → resolve {false_positive:true} 回写误报 → FPR Gauge 重算        │
│                                                                                   │
└───────────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 单轮检测漏斗（确定性核心）

```
  采集 collect           L0 抑制           L1 检测                 L2 关联
 ──────────────▶ ──────────────▶ ───────────────────▶ ──────────────────────────▶
 从 source_url 拉取   维护窗口/黑名单    static_threshold     按 service 同源关联
 指标/日志信号        batch_check 批量    simple_compare        + 变更关联
 （http_metrics /     命中即丢            signature_aggregate   模板摘要（LLM 可选，默认不接）
   http_logs / mock）
  watermark 推进 / 写 signal_snapshot
          │                                        │
          ▼                                        ▼
  MetricSignal / LogSignal                  MetricAnomaly / LogAnomaly
          │
          └────────────┐
                       ▼
   L3 验证 l3_verify        emit
  ───────────────────▶ ────────────────────────────────▶
  per-key consecutive    取号 PR-YYYYMMDD-NNNN（SequenceStore）
  ≥ persistence_rounds   组装 ProblemRecord
  （默认 2 轮才开单）     write_or_append 原子去重落库
  fpr 误报率闸门（降级    severity 组合升 critical（M6）
  不丢弃）
                       │
                       ▼
               problem_record（state=pending）
```

> **开单的关键门槛（演示时最容易踩）**：L3 持续性按 `anomaly_key` 统计**连续出现轮数**，
> `verify.persistence_rounds` 默认为 2。即同一个异常**连续 2 轮都出现**，第 2 轮才会落 `problem_record`。
> 手动演示时同一端点要**触发两轮**才能看到问题单。

### 1.3 调度与恢复闭环

```
   ┌────────────────────────────────────────────────────────────────┐
   │  Scheduler（默认 APM_ENABLE_SCHEDULER=true）                    │
   │  每 tick (1s)：                                                 │
   │   1. lease 门：多副本只让一个副本调度（MySQL 原子接管）           │
   │   2. 找 due 的 monitor_target（按 schedule.interval_sec）        │
   │   3. 按 (tenant_id, domain) 分组 → Semaphore 限流               │
   │   4. run_round → 打点 + 写 detection_round + 审计日志            │
   └────────────────────────────────────────────────────────────────┘
                             │
                             ▼
   run_round（poller.py）
     collect（并行，单源失败 → degraded_sources 不崩溃）
       → run_domain（L0→L1→L2→L3→emit，一条 trace_id 贯穿）
       → 写 detection_round（running → success/partial/failed）
                             │
                             ▼
   Reconciler（周期 30s，APM_RESOLVE_AFTER_ROUNDS=3）
     扫 pending 单 → 各 anomaly_key 从 detection_state 看连续 miss
     → 全部达标 → resolve(reason="auto") 自动关单
```

### 1.4 数据 / 接口关系（谁读谁）

| 配置对象 | 存于表 | 被谁消费 |
|---------|--------|---------|
| monitor_target | `monitor_target` | 调度器 / poller / `/v1/monitors` |
| domain_config | `domain_config` | `build_context` 每轮载入（L1/L3 规则） |
| maintenance_window | `maintenance_window` | `build_context` → L0 抑制 |
| suppress_blacklist | `suppress_blacklist` | `build_context` → L0 抑制 |
| fpr_table | `fpr_table` | `build_context` → L3 误报率闸门；resolve 回写 |
| change_record | `change_record` | `build_context` → L2 变更关联 |
| problem_record | `problem_record` | `/v1/problems`、Reconciler |
| detection_round | `detection_round` | `/v1/audit/rounds`、`/v1/audit/suppressed` |
| signal_snapshot / detection_state / collect_watermark | 对应表 | 采集幂等去重 / L3 持续性 / 增量水位 |

---

## 2. 需要哪些「页面」

> **说明**：本项目 M0 明确**不做前端**。当前系统的「页面 / 界面」就是 **REST API**（服务端解析 `X-Tenant-Id` 请求头，
> 默认租户 `default`）。下表即操作者使用的全部「页面」；实现计划中规划的前端菜单树见 §2.2（仅设计，未实现）。

### 2.1 当前操作面 = REST API 清单

**鉴权说明**：配置了 `APM_API_KEYS` 才强制鉴权；未配置 = 放行（匿名 admin）。标注「admin」的端点需 master key（`"*"` 全租户）。

| 分组 | 端点 | 方法 | 用途 | 鉴权 |
|------|------|------|------|------|
| 系统探针 | `/health` | GET | 存活探针 | 公开 |
| 系统探针 | `/ready` | GET | 就绪探针（db + plugins 两检查） | 公开 |
| 指标 | `/metrics` | GET | Prometheus 指标 | 公开 |
| 监控管理 | `/v1/monitors` | POST | 新建监控端点（先过 SSRF 网关） | 租户 |
| 监控管理 | `/v1/monitors` | GET | 列端点（?service= / ?signal_type=） | 租户 |
| 监控管理 | `/v1/monitors/{id}` | GET/PUT/DELETE | 详情 / 更新 / 软删 | 租户 |
| 监控管理 | `/v1/monitors/{id}/test` | POST | 连通性测试（一次采集不写库） | 租户 |
| 监控管理 | `/v1/monitors/{id}/run` | POST | 手动单跑（采集 + 漏斗） | 租户 |
| 插件管理 | `/v1/plugins` | GET | 已加载插件清单 | 租户 |
| 插件管理 | `/v1/plugins/reload` | POST | 重扫 entry_points 热替换 | **admin** |
| 告警触发 | `/v1/alerts/run` | POST | 全量跑一轮（?domain= 过滤） | **admin** |
| 问题单 | `/v1/problems` | GET | 查问题单（state/service/severity/limit） | 租户 |
| 问题单 | `/v1/problems/{id}` | GET | 问题单详情 | 租户 |
| 问题单 | `/v1/problems/{id}/resolve` | POST | 手动关单（可选 body `{"false_positive":true}` 回写误报） | 租户 |
| 配置 | `/v1/config/reload` | POST | 重载插件 registry（admin） | **admin** |
| 配置 | `/v1/config/{domain}` | GET | 读某域检测规则 | 租户 |
| 配置 | `/v1/config/{domain}` | PUT | 写某域检测规则（参数校验，非法→400 CONFIG_ERROR） | **admin** |
| 维护窗口 | `/v1/maintenance-windows` | POST/GET | 新建 / 列出（?service=） | 租户 |
| 维护窗口 | `/v1/maintenance-windows/{id}` | PUT/DELETE | 更新 / 删除 | 租户 |
| 黑名单 | `/v1/blacklist` | POST/GET | 新建 / 列出 | 租户 |
| 黑名单 | `/v1/blacklist/{id}` | PUT/DELETE | 更新（可启停）/ 删除 | 租户 |
| 审计 | `/v1/audit/rounds` | GET | 轮次审计（domain/status/limit/offset） | 租户 |
| 审计 | `/v1/audit/suppressed` | GET | 被抑制信号摊平（?service=） | 租户 |

### 2.2 实现计划中的前端页面（未实现，仅设计）

实现计划（`apm-alert-implementation-plan-enhanced.md` 附录 A）定义了以下前端菜单树。**当前仓库未实现任何前端**；
若后续开发前端，以下即页面清单，每个页面对应的后端 API 见 §2.1 表格。

```
APM 告警管理系统
├── 告警管理
│   ├── 问题列表（ProblemListPage）        ← GET /v1/problems
│   ├── 问题详情（ProblemDetailPage）      ← GET /v1/problems/{id} + POST resolve
│   └── 检测状态（DetectionStatePage）     ← 内部 detection_state 表
├── 监控管理
│   ├── 监控端点列表（MonitorListPage）    ← GET /v1/monitors
│   ├── 新建端点（MonitorFormPage）        ← POST /v1/monitors
│   ├── 端点详情（MonitorDetailPage）      ← GET/PUT/DELETE /v1/monitors/{id}
│   └── 采集测试（CollectorTestPage）      ← POST /v1/monitors/{id}/test
├── 配置管理
│   ├── 检测规则（DomainConfigPage）       ← GET/PUT /v1/config/{domain}
│   ├── 规则版本历史（ConfigVersionPage）
│   ├── 维护窗口（MaintenanceWindowPage）  ← /v1/maintenance-windows
│   ├── 黑名单（BlacklistPage）            ← /v1/blacklist
│   └── 误报率（FprPage）                  ← fpr_table + /v1/problems resolve
├── 系统管理
│   ├── 插件管理（PluginListPage）         ← GET /v1/plugins + POST reload
│   ├── 调度状态（SchedulerStatusPage）
│   ├── 数据库迁移（MigrationPage）        ← make migrate
│   ├── 数据库状态（DBStatusPage）         ← GET /ready
│   └── 安全审计（SecurityAuditPage）      ← SecurityAudit 日志
├── 监控仪表盘
│   ├── 总览（DashboardPage）              ← /metrics（Prometheus 面板）
│   ├── 轮次审计（RoundAuditPage）         ← GET /v1/audit/rounds
│   └── 抑制审计（SuppressedAuditPage）    ← GET /v1/audit/suppressed
├── 手动操作
│   └── 触发检测（TriggerPage）            ← POST /v1/alerts/run / /v1/monitors/{id}/run
└── 系统
    ├── 健康状态（HealthPage）             ← GET /health
    └── 错误页（ErrorPage）                ← 统一异常响应 {code, reason, trace_id}
```

---

## 3. 如何配置

系统配置分四层：**环境变量**（进程级）、**monitor_target**（监控对象）、**domain_config**（检测规则）、
**动态配置**（维护窗口 / 黑名单 / 误报）。外加**插件**与**鉴权**。

### 3.1 环境变量（`APM_` 前缀，`.env` 文件或真实环境变量）

| 变量 | 默认 | 说明 |
|------|------|------|
| `APM_HOST` | `0.0.0.0` | 监听地址 |
| `APM_PORT` | `8000` | 监听端口（`.env.example` 预置 7070） |
| `APM_STORAGE_BACKEND` | `mysql` | `mysql`（生产）/ `memory`（本地 demo/单测） |
| `APM_DB_HOST/PORT/USER/PASSWORD/NAME` | `127.0.0.1/3306/root//aiops_apm_runtime` | MySQL 连接 |
| `APM_ENABLE_SCHEDULER` | `true` | 是否启动调度器 / Reconciler 后台任务 |
| `APM_SCHEDULER_TICK_SEC` | `1.0` | 调度 tick 间隔（秒） |
| `APM_MAX_CONCURRENT_ROUNDS` | `10` | 并行轮次上限 |
| `APM_TOTAL_TIMEOUT_SEC` | `30.0` | 单轮总超时 |
| `APM_SCHEDULER_LEASE_TTL_SEC` | `30.0` | 多副本 lease 有效期（MySQL） |
| `APM_RESOLVE_AFTER_ROUNDS` | `3` | Reconciler 连续 miss 多少轮自动关单 |
| `APM_RESOLVE_CHECK_INTERVAL_SEC` | `30.0` | Reconciler 扫描间隔 |
| `APM_API_KEYS` | 空 | 鉴权 JSON，如 `{"k1":"tenant-a","k2":"*"}`；空=放行 |
| `APM_AUDIT_ENABLED` | `true` | 安全审计日志开关 |
| `APM_OUTBOUND_TIMEOUT_SEC` | `10.0` | 出站 HTTP 超时 |
| `APM_OUTBOUND_MAX_BODY_BYTES` | `5000000` | 出站响应体上限 |
| `APM_ENABLE_LLM_SUMMARY` | `false` | L2 摘要钩子（不接真实 LLM，默认模板摘要） |

### 3.2 对象配置 ①：monitor_target（监控谁、从哪采、多快采）

`POST /v1/monitors` 创建。**必填**：`service`、`signal_type`、`source_type`、`source_config`。

| 字段 | 说明 |
|------|------|
| `service` | 服务名（如 `order-management`），用于按 service 分组开单 |
| `signal_type` | `metric` / `log` / `change` |
| `source_type` | `prometheus` / `http` / `elk` / `mock`（分派矩阵：metric+prometheus→http_metrics；log+http/elk→http_logs；mock→MockCollector） |
| `domain` | 域（默认 `application`），决定用哪个 `domain_config` 规则 |
| `source_config.url` | 采集源地址。**必过 SSRF 网关**：仅 http/https；拦截 127/8、10/8、172.16/12、192.168/16、169.254/16、::1；域名解析后任一 IP 命中私网即拒（fail-closed） |
| `source_config.rows_path` | 响应里数组行的点路径（默认 `data.result`，Prometheus instant query 形状） |
| `source_config.field_mapping` | 行 → 信号字段映射（`metric`/`value`/`timestamp`/`service`/`level`/`message`/`stack_trace`），支持点路径与 `value[1]` 数组索引 |
| `source_config.headers` | 请求头；`authorization`/`x-api-key` 必须用 `${env:X}` 或 `${vault:path#key}` 引用（拒明文凭据） |
| `source_config.params` | 额外查询参数（指标采集还会下推 `start` 水位线） |
| `source_config.signature_frames` | 日志堆栈签名帧数（默认 3） |
| `schedule.interval_sec` | 调度间隔（默认 60s） |
| `enabled` | 是否启用（默认 true；DELETE 为软删 enabled=0） |

**field_mapping 两种常用源形状：**

- Prometheus instant query（`rows_path` 默认 `data.result`）：
  ```json
  {"metric": "metric.__name__", "value": "value[1]", "timestamp": "value[0]", "service": "service"}
  ```
  对应响应行：`{"metric": {"__name__": "cpu_usage"}, "service": "demo-app", "value": [1699999999, "0.95"]}`
- 自定义 JSON（行本身即字段）：
  ```json
  {"metric": "metric", "value": "value", "timestamp": "timestamp", "service": "service"}
  ```

> 注意：`field_mapping` 按**每一行**映射，`service`/`timestamp` 若在顶层而不在行内，将取不到而回退默认
> （service→`unknown`，timestamp→报错降级）。演示/生产源需把 `service`、`timestamp` 放进每一行。

### 3.3 对象配置 ②：domain_config（怎么判、怎么抑制、怎么验证）

`GET /v1/config/{domain}` 读；`PUT /v1/config/{domain}` 写（**admin**，写入侧校验非法→400 `CONFIG_ERROR`）。
空表时会用 `src/aiops_apm/config/domains.yaml` seed（`application` 域）。

结构（`src/aiops_apm/models/config.py`）：

```json
{
  "detectors": [
    {"signal": "cpu_usage", "plugin": "static_threshold",   "params": {"threshold": 0.9},                "severity": "high"},
    {"signal": "error_rate", "plugin": "simple_compare",    "params": {"ratio": 1.5, "baseline": 0.02},  "severity": "high"},
    {"signal": "ERROR",      "plugin": "signature_aggregate","params": {"min_count": 5, "n_frames": 3},   "severity": "warning"}
  ],
  "suppressors": [
    {"name": "maintenance_window", "params": {}},
    {"name": "blacklist",          "params": {}}
  ],
  "correlation": {"metric_log_window_sec": 300, "change_window_sec": 300},
  "verify":      {"persistence_rounds": 2, "false_positive_threshold": 0.6, "min_samples": 20}
}
```

| 插件 | 参数（写入侧校验） | 说明 |
|------|-------------------|------|
| `static_threshold` | `threshold`（必填）、`operator`（gt/gte/lt/lte/range）、`range:[lo,hi]` | 值按 operator 越过阈值 → MetricAnomaly |
| `simple_compare` | `baseline` 或 `ratio`（至少一个；ratio 须为正） | `value > baseline * ratio` |
| `signature_aggregate` | `min_count`、`n_frames`（须为正） | 同堆栈签名日志数 ≥ min_count → 1 条 LogAnomaly |
| `maintenance_window` | `duration_minutes`（须为正） | L0 维护窗口抑制 |
| `blacklist` | `pattern`（非空串） | L0 黑名单抑制 |

### 3.4 对象配置 ③：动态配置（维护窗口 / 黑名单 / 误报回写）

每轮 `build_context` 从表读取，改配置即时生效（无需重启）。

- **维护窗口** `POST /v1/maintenance-windows`：
  ```json
  {"service": "demo-app", "start_at": "2026-08-27T11:00:00Z", "end_at": "2026-08-27T12:00:00Z", "reason": "release"}
  ```
  命中窗口内该 service 的信号在 L0 被抑制。
- **黑名单** `POST /v1/blacklist`：
  ```json
  {"domain": "application", "service": "demo-app", "signal": "cpu_usage", "reason": "known noisy"}
  ```
  命中在 L0 被抑制（可按启停 `enabled` 开关）。
- **误报回写** `POST /v1/problems/{id}/resolve` body `{"false_positive": true}`：
  该单 `group_key` 在 `fpr_table` 记一次误报（total+1、fpr 重算），并更新 `aiops_false_positive_rate` Gauge。

### 3.5 插件（可插拔规则）

- 三个 `entry_points` 组：`aiops_apm.collectors` / `aiops_apm.detectors` / `aiops_apm.suppressors`，每个 entry 指向 `build() -> Plugin` 工厂。
- 内置：collector 3（`http_logs`/`http_metrics`/`mock`）、detector 3（`static_threshold`/`simple_compare`/`signature_aggregate`）、suppressor 2（`maintenance_window`/`blacklist`）。
- 启动时 registry 自动发现；`POST /v1/plugins/reload` 重扫 entry_points **原子替换**快照（正在跑的轮次继续用旧快照，不中断）。
- 第三方插件示例见 `docker/custom_detector/`（p95_latency）。

### 3.6 鉴权（配置了才强制）

- `APM_API_KEYS` 为空 = 不挂 AuthMiddleware，全部放行（匿名 admin）。
- 配置后：`Authorization: Bearer <key>` → scope（`"*"` 全租户，master key = admin）→ 无/错 key→401、`X-Tenant-Id` 超 scope→403。
- 示例：`APM_API_KEYS='{"k1":"tenant-a","k2":"*"}'`。

### 3.7 配置链路全景

```
写路径（人/平台）                     每轮读路径（服务）                     输出路径
.env/Settings ────────────────► Settings（app.state.settings）
POST /v1/monitors ────────────► monitor_target ──► Scheduler/Poller（采谁）
PUT  /v1/config/{domain} ─────► domain_config ────► build_context（L1/L3 规则）
POST /v1/maintenance-windows ─► maintenance_window─► build_context（L0 抑制）
POST /v1/blacklist ───────────► suppress_blacklist─► build_context（L0 抑制）
resolve{fp:true} ─────────────► fpr_table ─────────► build_context（L3 闸门）＋ FPR Gauge
                                            │
                                            ▼
                                  run_round（collect → L0→L1→L2→L3 → emit）
                                            │
                                            ▼
                                  problem_record ──► /v1/problems、Reconciler
```

---

## 4. 一步一步手动配置与跑通

两条路径，先易后难：

- **路径 A（零外部依赖，推荐先跑通链路）**：`memory` backend，不依赖 MySQL/网络，纯 `curl` 走通「启动 → 探针 → 插件 → 监控端点 → 配置 → 手动跑 → 审计/指标」。`mock` 采集器经 API 建的端点不产信号，因此**用于验证链路通、轮次审计、指标打点**；要看真实告警见路径 B。
- **路径 B（产真实告警）**：本地起一个 Prometheus 形状的演示源 + 临时演示豁免 SSRF 私网拦截（**仅本地演示，生产必须恢复**），`http_metrics` 采集到超阈值信号，跑两轮 → 看到 `problem_record` → resolve 误报回写 → 维护窗口抑制 → 恢复自动关单。
- **路径 C（生产形态，可选）**：切换 MySQL backend + `make migrate`（其余操作同路径 B）。

> 约定：以下 `<port>` 指 `APM_PORT`（示例统一用 `7070`）；所有请求都带 `-H "X-Tenant-Id: default"`（可省，缺省即 `default`）。

### 4.1 路径 A：本地零依赖链路验证（memory backend）

**Step A0 — 安装依赖（首次）**

```bash
cd /Users/h.a.hu/accenture/accenture_aiops_platform/acc-aiops-platform-zjb/aiops-apm-anomaly-detector
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"      # 或 make install
```

**Step A1 — 配置 `.env`（memory + 先关调度，方便手动逐步触发）**

```bash
cp .env.example .env    # 若已存在则直接编辑
```

在 `.env` 中设置（注意 `make dev`/`make migrate` 都会 source `.env`，`.env` 优先级高于已导出的变量）：

```bash
APM_STORAGE_BACKEND=memory
APM_ENABLE_SCHEDULER=false
APM_PORT=7070
```

> `APM_ENABLE_SCHEDULER=false` 只是先关掉自动调度，改用手动 `POST .../run`，步骤更可控；跑通后想体验自动调度可改回 `true`。

**Step A2 — 启动服务**

```bash
make dev
```

看到 `Uvicorn running on http://0.0.0.0:7070` 即成功。另开一个终端做后续请求。

**Step A3 — 探针**

```bash
curl -i http://127.0.0.1:7070/health        # 200 {"status":"ok"}
curl -i http://127.0.0.1:7070/ready         # 200 {"status":"ready","checks":{"db":true,"plugins":true}}
```

**Step A4 — 插件清单**

```bash
curl -i http://127.0.0.1:7070/v1/plugins
# → {"collector":["http_logs","http_metrics","mock"],
#    "detector":["signature_aggregate","simple_compare","static_threshold"],
#    "suppressor":["blacklist","maintenance_window"]}
```

**Step A5 — 建监控端点（mock；url 需过一个合法非私网地址过网关，MockCollector 实际不请求它）**

```bash
# 指标端点 → 201 {"target_id":"MT-0001"}
curl -i -X POST http://127.0.0.1:7070/v1/monitors \
  -H "Content-Type: application/json" -H "X-Tenant-Id: default" \
  -d '{"service":"demo-app","signal_type":"metric","source_type":"mock","domain":"application",
       "source_config":{"url":"http://8.8.8.8/metrics"},
       "schedule":{"interval_sec":60},"enabled":true}'

# 日志端点 → 201 {"target_id":"MT-0002"}
curl -i -X POST http://127.0.0.1:7070/v1/monitors \
  -H "Content-Type: application/json" -H "X-Tenant-Id: default" \
  -d '{"service":"demo-app","signal_type":"log","source_type":"mock","domain":"application",
       "source_config":{"url":"http://8.8.8.8/logs"},
       "schedule":{"interval_sec":60},"enabled":true}'

# 列出
curl -i "http://127.0.0.1:7070/v1/monitors"
```

**Step A6 — 检测规则（自动 seed，可读可改）**

```bash
curl -i http://127.0.0.1:7070/v1/config/application
# → 200，返回 domains.yaml seed 的 application 域规则（cpu_usage/error_rate/ERROR 三个 detector + verify.persistence_rounds=2）

# 可选：改 verify 为 1 轮即开单（演示更快；生产按需）
curl -i -X PUT http://127.0.0.1:7070/v1/config/application \
  -H "Content-Type: application/json" \
  -d '{"detectors":[{"signal":"cpu_usage","plugin":"static_threshold","params":{"threshold":0.9},"severity":"high"}],
       "suppressors":[],"verify":{"persistence_rounds":1}}'
# → 200 {"domain":"application","version":2}
```

**Step A7 — 手动全跑一轮**

```bash
curl -i -X POST "http://127.0.0.1:7070/v1/alerts/run"
# → 200，rounds 里该域 target_count=2；mock 无信号 → record_count=0、degraded_sources=[]。
#   链路已通（采集→漏斗→审计→打点），只是 mock 端点不产信号。
```

**Step A8 — 审计与指标**

```bash
curl -i "http://127.0.0.1:7070/v1/audit/rounds?domain=application"   # 能看到 success 轮次
curl -i "http://127.0.0.1:7070/v1/audit/suppressed"                  # 被抑制信号摊平（无信号则空）
curl -i http://127.0.0.1:7070/metrics                                 # aiops_round_total 等已打点
curl -i "http://127.0.0.1:7070/v1/problems"                           # 空 items（mock 无信号）
```

✅ 到此路径 A 全链路走通。**想看到真实 `problem_record`**：a) 直接跑单测 `make test`（351 用例含 UC-5.x 端到端产单）；或 b) 走路径 B 接真实 HTTP 源。

### 4.2 路径 B：产真实告警（本地演示源 + 演示豁免 SSRF）

**Step B0 — 准备一个 Prometheus 形状的本地演示源**（`cpu_usage` 持续 0.95，超过默认阈值 0.9）

```bash
cat > /tmp/demo_source.py <<'PY'
import json, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        now = datetime.now(timezone.utc).timestamp()
        body = json.dumps({
            "status": "success",
            "data": {"resultType": "vector", "result": [
                {"metric": {"__name__": "cpu_usage"}, "service": "demo-app",
                 "value": [now, "0.95"]},
            ]},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

HTTPServer(("0.0.0.0", 9100), H).serve_forever()
PY
python /tmp/demo_source.py &     # 后台起在 :9100
curl -s http://127.0.0.1:9100/query | python -m json.tool   # 自检
```

**Step B1 — 演示豁免出站网关的本地地址拦截（仅本地演示！生产必须恢复）**

默认网关拦截 `127.0.0.0/8` 与 `::1`，本地演示源会被拒。临时放开（`make dev` 用了 `--reload`，保存即自动重启生效）：

- 编辑 `src/aiops_apm/collectors/_gateway.py`，把 `BLOCKED_NETWORKS` 里这两行注释掉：
  ```python
  # ipaddress.ip_network("127.0.0.0/8"),
  # ipaddress.ip_network("::1/128"),
  ```
- 保存后等 uvicorn 自动 reload。
- **演示结束务必还原这两行**（生产环境 SSRF 拦截必须保留）。

**Step B2 — 建 `http_metrics` 监控端点，指向本地演示源**

```bash
curl -i -X POST http://127.0.0.1:7070/v1/monitors \
  -H "Content-Type: application/json" -H "X-Tenant-Id: default" \
  -d '{"service":"demo-app","signal_type":"metric","source_type":"prometheus","domain":"application",
       "source_config":{"url":"http://127.0.0.1:9100/query?query=cpu_usage",
                        "field_mapping":{"metric":"metric.__name__","value":"value[1]","timestamp":"value[0]","service":"service"}},
       "schedule":{"interval_sec":60},"enabled":true}'
# → 201 {"target_id":"MT-0001"}
```

**Step B3 — 连通性测试（一次采集，不写库）**

```bash
curl -i -X POST http://127.0.0.1:7070/v1/monitors/MT-0001/test
# → 200 {"status":"ok","signal_count":1,"signals":[{... "metric":"cpu_usage","value":0.95 ...}]}
```

**Step B4 — 跑两轮（L3 持续性默认 2 轮才开单）**

```bash
curl -i -X POST http://127.0.0.1:7070/v1/monitors/MT-0001/run
# 第 1 轮：anomaly_count=1，但 record_created=0（consecutive=1 < persistence_rounds=2）

curl -i -X POST http://127.0.0.1:7070/v1/monitors/MT-0001/run
# 第 2 轮：consecutive=2 → 落单，records 里出现 record_id，如 PR-20260827-0001
```

**Step B5 — 查问题单**

```bash
curl -i "http://127.0.0.1:7070/v1/problems?state=pending"
# → items 里一条：service=demo-app, severity=high, metric_anomalies=[cpu_usage=0.95]
curl -i http://127.0.0.1:7070/v1/problems/PR-20260827-0001     # 详情（symptom/verification/trace_id）
```

**Step B6 — 误报回写（resolve 记一次误报 → fpr_table + FPR Gauge）**

```bash
curl -i -X POST http://127.0.0.1:7070/v1/problems/PR-20260827-0001/resolve \
  -H "Content-Type: application/json" -d '{"false_positive":true}'
# → {"record_id":"...","state":"resolved","false_positive_recorded":true}
curl -i http://127.0.0.1:7070/metrics | grep aiops_false_positive_rate
```

**Step B7（可选）— 维护窗口抑制**：先给演示源覆盖的 service 建一个「现在」在窗口内的维护窗口，再跑一轮 → L0 抑制、不开单：

```bash
START=$(date -u -v-10M +%Y-%m-%dT%H:%M:%SZ)   # macOS；Linux 用 date -u -d '-10 min' +%Y-%m-%dT%H:%M:%SZ
END=$(date -u -v+10M +%Y-%m-%dT%H:%M:%SZ)
curl -i -X POST http://127.0.0.1:7070/v1/maintenance-windows -H "Content-Type: application/json" \
  -d "{\"service\":\"demo-app\",\"start_at\":\"$START\",\"end_at\":\"$END\",\"reason\":\"demo\"}"
curl -i -X POST http://127.0.0.1:7070/v1/monitors/MT-0001/run      # timeline.suppressed.count>0
curl -i "http://127.0.0.1:7070/v1/audit/suppressed?service=demo-app"
```

**Step B8（可选）— 恢复与自动关单**：把演示源值降到 0.3（低于阈值），连跑 N 轮（N ≥ `APM_RESOLVE_AFTER_ROUNDS`=3），
Reconciler 会检测到所有 anomaly_key 连续 miss → `resolve(reason="auto")` 自动关单。本步需把调度器或 Reconciler 打开
（`APM_ENABLE_SCHEDULER=true`），或等其周期扫描。

✅ 路径 B 产出真实 `problem_record`，全链路（采集→L0→L1→L2→L3→emit→查询→误报回写）跑通。

### 4.3 路径 C（可选）：切换 MySQL 生产形态

仅需把 backend 换成 `mysql` 并建库建表，其余操作同路径 B：

```bash
# 1) 确保本机 MySQL 运行（brew services start mysql 或 docker）
# 2) .env 设置
APM_STORAGE_BACKEND=mysql
APM_DB_HOST=127.0.0.1
APM_DB_PORT=3306
APM_DB_USER=root
APM_DB_PASSWORD=root123
APM_DB_NAME=aiops_apm_runtime
# 3) 建库建表（幂等）
make migrate
# 4) 启动 + 按路径 B 操作（memory/mysql 对 API 无差别）
make dev
```

> 本机 MySQL 未运行会触发 **fail-fast**：`build_storage` 连不上 DB → uvicorn 启动即退出（memory backend 无此约束）。
> 另可试一键环境 `make docker-up`（mysql+mock-source+apm-alert+prometheus），见 §6 已知边界。

---

## 5. 端到端验证清单（Checklist）

| # | 验证项 | 操作 | 预期 |
|---|--------|------|------|
| 1 | 存活 | `GET /health` | 200 `{"status":"ok"}` |
| 2 | 就绪 | `GET /ready` | 200 `{"db":true,"plugins":true}` |
| 3 | 插件 | `GET /v1/plugins` | 3 collector / 3 detector / 2 suppressor |
| 4 | 端点 CRUD | `POST/GET/PUT/DELETE /v1/monitors` | 201 建 / 200 列 / 200 改 / 204 软删 |
| 5 | SSRF 拦截 | 建 url 为 `http://169.254.169.254/...` 端点 | 400 `blocked network` |
| 6 | 连通测试 | `POST /v1/monitors/{id}/test` | 200 返回信号样本 |
| 7 | 配置 seed/校验 | `GET /v1/config/application`；`PUT` 非法参数 | 200；400 `CONFIG_ERROR` |
| 8 | 手动跑 | `POST /v1/monitors/{id}/run` 两次 | 第 2 轮 `record_created=1` |
| 9 | 问题单 | `GET /v1/problems` | 出现 `PR-...`（severity=high） |
| 10 | 误报回写 | `POST /v1/problems/{id}/resolve {"false_positive":true}` | `false_positive_recorded:true`；`/metrics` FPR Gauge 变化 |
| 11 | 维护窗口抑制 | 建窗口 + run | timeline.suppressed 命中；`/v1/audit/suppressed` 可见 |
| 12 | 黑名单抑制 | 建黑名单 + run | L0 抑制 |
| 13 | 轮次审计 | `GET /v1/audit/rounds?domain=...` | 每轮一条，status=success |
| 14 | 指标 | `GET /metrics` | `aiops_round_total`/`aiops_records_created` 等出现 |
| 15 | 鉴权 401/403 | 配 `APM_API_KEYS` 后无 key / 跨租户 | 401 / 403 |
| 16 | 恢复自动关单 | 信号消失 ≥3 轮 | Reconciler 自动 `resolve(reason=auto)` |
| 17 | 质量门 | `make lint` / `make test` | 全绿（351 用例） |

---

## 6. 已知边界与常见问题

| 现象 / 边界 | 原因与处理 |
|------------|-----------|
| **`mock` 端点建了但跑不出告警** | `_mock_signals` 属测试私有字段，经 API/`monitor_target` 表存取会被丢弃（store 只保留公开字段）。API 建的 mock 端点恒 0 信号 → 用于链路验证，产告警请走路径 B。 |
| **出站网关拦截本地/内网** | `BLOCKED_NETWORKS` 含 `127.0.0.0/8`、`::1`、`10/8`、`172.16/12`、`192.168/16`、`169.254/16`；域名二次解析命中私网或解析失败均拒绝（fail-closed）。本地演示按 §4.2 Step B1 临时豁免，**生产必须保留**。 |
| **`docker compose up`（`make docker-up`）产不出告警** | M7 交付待补跑：① mock-source 在 compose 私网（172.16/12）会被网关拦截；② `docker/seed.py` 的 `source_config` 用了 `metric_path`/`log_path`，与采集器期望的 `rows_path`+`field_mapping` 不一致，且 `docker/mock_source.py` 每行缺 `timestamp`/`service` 供 field_mapping 逐行映射。环境可用后需按 §4.2 的配置形态修正 seed 与 mock_source。 |
| **MySQL 连不上启动即退出** | mysql backend 是 fail-fast（`build_storage` 抛异常）；memory backend 不受影响。检查 MySQL 是否运行、凭据、`APM_DB_NAME` 是否已建。 |
| **`make migrate` 报 `2003 Can't connect`** | 本机 MySQL 未运行（brew services 无 mysql / 3306 无监听）。启动 MySQL 后重试；V2/V3 迁移由单测覆盖，待 DB 可用补跑。 |
| **手动 run 第一次不开单** | L3 持续性 `persistence_rounds`（默认 2）：需连续 2 轮命中才落 `problem_record`。演示可临时把 verify 改 `{"persistence_rounds":1}`。 |
| **`PUT /v1/config/{domain}` 报 400 CONFIG_ERROR** | 写入侧参数校验：`static_threshold` 缺 `threshold`、`simple_compare` 缺 `baseline/ratio`、`signature_aggregate` 参数非正数、插件名不存在等。 |
| **`simple_compare` 基线不自动** | 基线来自 `params["baseline"]`；signal_snapshot 滚动均值注入为 M5 后续演进项。 |
| **`/v1/config/reload` 与 `/v1/config/{domain}` 路径** | reload 声明在 `{domain}` 之前避免路径冲突；两者均需 admin。 |
| **响应里 datetime 乱** | FastAPI 对 dict 自动 `jsonable_encoder` 序列化；手写 `JSONResponse(model_dump())` 会因 datetime 不可 JSON 序列化报错（既有代码无此问题）。 |
| **调 `/metrics` 慢** | `prometheus_client` 文本暴露，数据量大时正常。 |

---

## 7. 完整命令速查

```bash
# ── 工程 ─────────────────────────────────────────────
make install      # 建 .venv 并安装 [dev]
make lint         # ruff + mypy
make test         # pytest（351 用例）
make dev          # uvicorn 启动（读 .env，APM_STORAGE_BACKEND 选 backend）
make migrate      # 建齐 aiops_apm_runtime 全部表（需 MySQL）

# ── 探针 / 插件 ──────────────────────────────────────
curl -i http://127.0.0.1:7070/health
curl -i http://127.0.0.1:7070/ready
curl -i http://127.0.0.1:7070/v1/plugins
curl -i -X POST http://127.0.0.1:7070/v1/plugins/reload

# ── 监控端点 ─────────────────────────────────────────
curl -i -X POST http://127.0.0.1:7070/v1/monitors -H "Content-Type: application/json" \
  -d '{"service":"demo-app","signal_type":"metric","source_type":"prometheus","domain":"application",
       "source_config":{"url":"http://127.0.0.1:9100/query?query=cpu_usage",
                        "field_mapping":{"metric":"metric.__name__","value":"value[1]","timestamp":"value[0]"}},
       "schedule":{"interval_sec":60},"enabled":true}'
curl -i "http://127.0.0.1:7070/v1/monitors"
curl -i http://127.0.0.1:7070/v1/monitors/MT-0001
curl -i -X POST http://127.0.0.1:7070/v1/monitors/MT-0001/test
curl -i -X POST http://127.0.0.1:7070/v1/monitors/MT-0001/run   # 连跑两次才开单

# ── 触发 / 问题 ──────────────────────────────────────
curl -i -X POST "http://127.0.0.1:7070/v1/alerts/run?domain=application"
curl -i "http://127.0.0.1:7070/v1/problems?state=pending&severity=high"
curl -i http://127.0.0.1:7070/v1/problems/PR-20260827-0001
curl -i -X POST http://127.0.0.1:7070/v1/problems/PR-20260827-0001/resolve \
  -H "Content-Type: application/json" -d '{"false_positive":true}'

# ── 配置 / 动态配置 ──────────────────────────────────
curl -i http://127.0.0.1:7070/v1/config/application
curl -i -X PUT http://127.0.0.1:7070/v1/config/application -H "Content-Type: application/json" \
  -d '{"detectors":[{"signal":"cpu_usage","plugin":"static_threshold","params":{"threshold":0.9},"severity":"high"}],"verify":{"persistence_rounds":1}}'
curl -i -X POST http://127.0.0.1:7070/v1/maintenance-windows -H "Content-Type: application/json" \
  -d '{"service":"demo-app","start_at":"2026-08-27T11:00:00Z","end_at":"2026-08-27T12:00:00Z","reason":"release"}'
curl -i -X POST http://127.0.0.1:7070/v1/blacklist -H "Content-Type: application/json" \
  -d '{"domain":"application","service":"demo-app","signal":"cpu_usage","reason":"noisy"}'

# ── 审计 / 指标 ──────────────────────────────────────
curl -i "http://127.0.0.1:7070/v1/audit/rounds?domain=application&status=success"
curl -i "http://127.0.0.1:7070/v1/audit/suppressed?service=demo-app"
curl -i http://127.0.0.1:7070/metrics

# ── 鉴权（配置 APM_API_KEYS 后）─────────────────────
curl -i http://127.0.0.1:7070/v1/monitors -H "Authorization: Bearer k1" -H "X-Tenant-Id: tenant-a"
curl -i http://127.0.0.1:7070/v1/monitors -H "Authorization: Bearer k2"    # "*" 全租户
```
