# Project Overview — aiops-apm-anomaly-detector 项目全貌速览（一页卡）

> 本页是项目的「全貌一页卡」：**新会话 / 新成员**先用它 5 分钟了解项目是什么、当前状态、架构、续接点，再进行 enhance / 问答。
> 新会话续接请重点看 §2「当前状态」、§8「续接指引」、§9「已知边界」。
>
> 详细事实源：设计 [`docs/apm-alert-module-design.md`](apm-alert-module-design.md) / 实现计划 [`docs/apm-alert-implementation-plan-enhanced.md`](apm-alert-implementation-plan-enhanced.md)
> · 实现日志 `docs/logs/*` · 各 M 计划 `docs/plans/*` · 端到端手动跑通手册 [`docs/operational-guide.md`](operational-guide.md)

## 1. 这是什么

**APM（应用性能监控）告警模块**：从第三方 API 采集指标/日志 → 经过**确定性 L0–L3 漏斗** → 产出 `problem_record` 落库，供下游诊断/修复使用。FastAPI + MySQL，Python 3.10+。

## 2. 当前状态（2026-08-27）

- **M0–M7 全部完成**：`make lint test dev` 全绿，**351 用例**通过。**M8 待定义**。
- **M0** 工程基座（Settings/异常/探针）· **M1** 契约层（models+fingerprint+plugins.base，**已冻结**）· **M2** 持久化/迁移（12 表+seed）· **M3** 采集层/出站网关（http_metrics/logs/mock + SSRF）· **M4** 检测层（registry+3 detector+2 suppressor）· **M5** 漏斗 L0–L3+emit（确定性核心）· **M6** 调度/多租户/API/恢复闭环 · **M7** 可观测性/安全加固/交付打包（指标+审计+docker）。
- **遗留 backlog（下一个可做项）**：① 真实 LLM L2 摘要（`summary.py` 钩子已备，`APM_ENABLE_LLM_SUMMARY` 开关）② vault 密钥管理（`${vault:...}` 为占位）③ MySQL 真库实测（rounds/fpr/V3 迁移，本机 MySQL 当前未运行）④ `docker compose up` / `locust` 端到端实测（已写出待补跑）。

## 3. 架构

- **Pipeline（每轮检测，单 `trace_id` 贯穿）**：
  `collect → L0 抑制（维护窗口/黑名单）→ L1 检测（static_threshold/simple_compare/signature_aggregate）→ L2 关联（同源+变更+模板摘要）→ L3 验证（持续性/误报率闸门/严重度）→ emit（去重落库）`
- **插件系统**：三个 `entry_points` 组（`aiops_apm.collectors/detectors/suppressors`），每个 entry 指向 `build() -> Plugin` 工厂；registry 原子快照热替换。
- **存储**：单 schema `aiops_apm_runtime`，全部表带 `tenant_id`；`APM_STORAGE_BACKEND`= `mysql` / `memory`（demo/单测）。
- **多租户**：`X-Tenant-Id` 请求头（默认 `default`），服务端解析、不信任 body。
- **技术栈**：Python 3.10+ · FastAPI（uvicorn `:8000`）· aiomysql · pydantic/pydantic-settings（`APM_` 前缀）· prometheus_client。

## 4. 四条不可回退设计原则（改动必须遵守）

1. **确定性优先** — 检测是「确定性 pipeline + 少量 LLM」；LLM 仅 L2 写现象摘要，**绝不参与检测决策**。
2. **可插拔规则** — 采集器/检测器/抑制器经 entry_points 动态加载；改配置即热插拔，无需重启。
3. **单一 `trace_id`** — 每轮一个 trace_id 贯穿采集→漏斗→落库，写进 `problem_record`。
4. **多租户隔离** — `tenant_id` 贯穿过滤配置/调度/采集/落库/查询。

## 5. 模块地图（`src/aiops_apm/`）

```
main.py / _app.py      入口（uvicorn aiops_apm.main:app）+ create_app + lifespan + 统一异常
settings.py / exceptions.py   APM_ 前缀配置；AppException + ErrorCode
config/                loader（DB 主源→seed→last-known-good）+ domains.yaml + validator（写入校验）
models/                signal / anomaly / record / config + fingerprint.py（anomaly_key/group_key 真源，M1 冻结）
plugins/               base（Plugin/Collector/Detector/Suppressor ABC）+ registry（entry_points 原子快照）
collectors/            http_metrics / http_logs / mock + _gateway（SSRF DNS 二次校验）+ _field_mapping + _http_client
detectors/             static_threshold / simple_compare / signature_aggregate
suppressors/           maintenance_window / blacklist
pipeline/              context / l0_suppress / l1_detect / l2_correlate / l3_verify / emit / runner / filter_signals
storage/               connection + records + domain_config + monitor_targets + snapshots + watermarks
                       + sequence + detection_state + dynamic_config + lease + rounds
router/                api + monitors + plugins + alerts + problems + config + maintenance + blacklist + audit
scheduler.py / poller.py / reconcile.py   自动调度 / 单轮编排 / 自动关单
auth/                  AuthMiddleware + Principal（配置了才强制）
metrics.py / audit.py / summary.py         Prometheus 指标 / 安全审计 / L2 摘要钩子
migrations/            V1(12 表) + V2(collect_watermark) + V3(detection_round.domain)
```

## 6. 常用命令

```bash
make install   # 建 .venv + 安装 [dev]（工具链：pip + venv，Makefile 内用 .venv/bin/*）
make lint      # ruff + mypy
make test      # pytest（351 用例）
make dev       # uvicorn 启动（读 .env，APM_PORT 端口，APM_STORAGE_BACKEND 选 backend）
make migrate   # 建齐 aiops_apm_runtime 全部表（需 MySQL）
make docker-up / docker-down / loadtest   # M7 交付（本机无 docker/locust → 待补跑）
```

## 7. 配置面（如何配）

- **环境变量**：`APM_PORT` / `APM_DB_*` / `APM_ENABLE_SCHEDULER` / `APM_API_KEYS`（鉴权 JSON，空=放行）/ `APM_STORAGE_BACKEND` / `APM_OUTBOUND_*` / `APM_AUDIT_ENABLED` 等。
- **monitor_target**（采什么）：`POST /v1/monitors`，字段 `service` / `signal_type` / `source_type` / `domain` / `source_config{url,rows_path,field_mapping,headers}` / `schedule{interval_sec}`。url 必过 SSRF 网关。
- **domain_config**（怎么判）：`PUT /v1/config/{domain}`，`detectors[{signal,plugin,params,severity}]` + `suppressors` + `correlation` + `verify{persistence_rounds,false_positive_threshold,min_samples}`；写入侧参数校验非法→400。
- **动态配置**：`/v1/maintenance-windows`、`/v1/blacklist`（L0 抑制）、`resolve{fp:true}`（误报回写 fpr_table）。
- **鉴权**：`APM_API_KEYS='{"k1":"tenant-a","k2":"*"}'`，`"*"` 全租户=admin；未配置=匿名 admin 放行。

## 8. 续接指引（enhance / Q&A）

- **当前焦点**：定义 **M8** + 清 backlog（真实 LLM L2、vault、MySQL 真库实测、docker compose / locust 端到端）。
- **每 M 流程（CLAUDE.md 规定）**：先出 `docs/plans/<M>-implementation-plan.md` → 完成后写 `docs/logs/<M>.md` → 归档已实现章节到 `docs/archive/` → 更新 README 进度表 → 更新本文件与 CLAUDE.md「当前里程碑」。
- **改动纪律**：M1 契约**已冻结**（后续只加可选字段、不改签名）；不违背四条不可回退原则；多租户硬约束（服务端解析 tenant）。
- **验收场景来源**：设计文档 §13 的 11 个用例（CPU 飙高、组合升 critical、OOM 聚合、同源关联、瞬时抖动、维护窗口、误报率闸门、日志源降级等）。

## 9. 已知边界（问答时先想到的坑）

- **L3 开单要连续 2 轮**：`verify.persistence_rounds` 默认 2，同一 anomaly_key 连续 2 轮命中第 2 轮才落 `problem_record`。
- **API 建的 mock 端点不产信号**：`_mock_signals` 为测试私有字段，经 store 会被丢弃 → mock 只用于链路验证；产告警需接真实 HTTP 源（`http_metrics`/`http_logs`）。
- **SSRF 网关拦截本地/私网**：`127.0.0.0/8`、`::1`、`10/8`、`172.16/12`、`192.168/16`、`169.254/16` 全拦，域名解析命中私网或解析失败也拒（fail-closed）。本地/容器演示需临时豁免（详见 operational-guide §4.2）。
- **`docker compose up` 产不出告警**（M7 待补跑遗留）：`seed.py` 用 `metric_path/log_path` 但采集器期望 `rows_path`+`field_mapping`；`mock_source.py` 每行缺 `timestamp/service`。
- **mysql backend 连不上 DB 启动即失败**（fail-fast）；memory backend 无此约束。

> 端到端手动跑通（流程图/页面/配置/逐步操作/验证清单）见 [`docs/operational-guide.md`](operational-guide.md)。
