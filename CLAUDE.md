# CLAUDE.md

本文件为 Claude Code（claude.ai/code）在本仓库中工作时提供指导。

## 项目概览

`aiops-apm-anomaly-detector` 是一个 APM（应用性能监控）告警模块。它从第三方 API 采集指标/日志，经过确定性的 L0–L3 漏斗，最终产出 `problem_record` 落库，供下游诊断/修复使用。

**当前里程碑：** M0 工程基座 + M1 契约层 + M2 持久化与迁移 + M3 采集层与出站网关 + M4 检测层（插件 registry + 内置 detector/suppressor）+ **M5 漏斗 L0–L3 + emit（确定性核心）已完成**（`make lint test dev` 全绿，225 用例）。已实现：工程骨架、`Settings`、`AppException`/`ErrorCode`、统一异常响应、探针（M0）；`models/`（signal/anomaly/record/config）+ `models/fingerprint.py`（去重真源）+ `plugins/base.py`（插件 ABC，契约冻结）（M1）；`migrations/`（`MigrationRunner` + `V1__init_tables.sql` 12 张表，`make migrate`）+ `storage/`（`ConnectionPool`/`RecordStore`/`DomainConfigStore`/`build_storage`，problem_record 原子去重）+ `config/`（`DomainConfigLoader` + `domains.yaml` seed）（M2）；`collectors/`（`OutboundGateway` 出站安全网关 + `SharedHttpClient` + `FieldMapper` + `http_metrics`/`http_logs`/`mock` 内置采集器，`collector_for` 分派）+ `MonitorTargetStore`/`SnapshotStore`/`WatermarkStore` + `signature.py`（L1 聚合共享）+ `V2__collect_watermark.sql` + `/v1/monitors` CRUD/连通性测试 API（M3）；`plugins/registry.py`（`PluginRegistry` 三组 entry_points 原子快照）+ `detectors/`（static_threshold/simple_compare/signature_aggregate）+ `suppressors/`（maintenance_window/blacklist）+ `pipeline/filter_signals.py`（结构化 matcher）+ `/v1/plugins` 列表/reload API（M4）；`pipeline/`（`DetectionContext`/`DomainResult`/`build_context`/`l0_suppress`/`l1_detect`/`l2_correlate`/`l3_verify`/`emit`/`run_domain`，确定性漏斗主体）+ `storage/`（`SequenceStore`/`DetectionStateStore`/`DynamicConfigStore`）（M5）。下一阶段（M6）：调度/多租户/API/恢复闭环——scheduler+poller、`/v1/problems` 与 `/v1/detection-state` API、用例 2（组合升 critical）、LLM L2 摘要、fpr 回写。

设计文档（`docs/apm-alert-module-design.md`、`docs/apm-alert-implementation-plan-enhanced.md`）是实现的事实来源与蓝图；已实现章节归档在 `docs/archive/`，实现日志在 `docs/logs/`，各 M 阶段实现计划在 `docs/plans/`。

## 事实来源文档

- `docs/apm-alert-module-design.md` — 详细设计：架构、L0–L3 漏斗、插件系统、数据模型（DDL）、配置 schema、API、调度器。
- `docs/apm-alert-implementation-plan-enhanced.md` — 分阶段实现计划（里程碑 M0–M7），含每个阶段的后端骨架、Use Case 清单与前端页面。
- `docs/全流程框架实现阶段映射总图.html` — 全流程与 M0–M7 里程碑的可视化映射图。

实现前先读这些文档，并按实现计划中定义的 M0→M7 顺序推进。

## 实现流程规则（M0–M7）

每个里程碑（M0–M7）落地实现时，必须遵循以下流程，保证文档随代码同步演进：

1. **实现计划**：每个 M 阶段动手前，先产出实现计划文档（范围、文件清单、验收标准）存入 `docs/plans/<M阶段>-implementation-plan.md`；并在本文件「当前里程碑」处标注该阶段「进行中」，完成后改为「已完成」。
2. **实现日志**：每完成一个 M 阶段，在 `docs/logs/` 下新建 `<M阶段>.md`（如 `docs/logs/M0.md`、`docs/logs/M1.md`），记录该阶段的改动点、新增/修改文件清单、完成状态与遗留问题。
3. **归档已实现内容**：从设计文档（`docs/apm-alert-module-design.md`、`docs/apm-alert-implementation-plan-enhanced.md`）中，把该阶段「已实现」的章节迁移到 `docs/archive/<M阶段>-<主题>.md`（按 M 阶段归档，如 `docs/archive/M5-funnel.md`）。原设计文档只保留尚未实现的部分。
4. **更新 README**：把已实现的内容同步更新到 `README.md`，使其持续反映实现进度（当前实现的模块、用法、目录结构、完成状态）。

## 四条不可回退的设计原则

1. **确定性优先** — 检测是「确定性 pipeline + 少量 LLM」（LLM 仅在 L2 写现象摘要，绝不参与检测决策）。L0/L1/L2/L3 均为确定性纯函数。
2. **可插拔规则** — 采集器、检测器、抑制器通过 Python `entry_points` 动态加载；配置在 MySQL 中以「插件名 + 参数」引用。改配置即热插拔，无需重启。
3. **单一 `trace_id`** — 每轮检测一个 `trace_id` 贯穿采集 → 漏斗 → 落库，写入 `problem_record` 做全链路追踪。
4. **多租户隔离** — `tenant_id`（HTTP 请求头 `X-Tenant-Id`，默认 `default`）贯穿全链路，用于过滤配置、调度、采集、落库与查询。

## 架构

- **技术栈**：Python 3.10+、FastAPI（uvicorn 启动，监听 `:8000`）、MySQL（`aiomysql`）、`pydantic`/`pydantic-settings`、`prometheus_client`。
- **Pipeline**（asyncio，每轮检测）：
  `collect → L0 抑制 → L1 检测 → L2 关联 → L3 验证 → emit`
  - L0 抑制（维护窗口 / 黑名单）→ L1 检测（静态阈值 / 环比 / 堆栈签名聚合）→ L2 关联（指标+日志同源、变更关联、可选 LLM 摘要）→ L3 验证（持续性、误报率闸门、严重度校准）→ emit（按 service 分组、去重、写入 `problem_record`）。
- **插件系统** — 三个 `entry_points` group，每个 entry 指向 `build() -> Plugin` 工厂函数：
  - `aiops_apm.collectors`、`aiops_apm.detectors`、`aiops_apm.suppressors`
  - 抽象基类在 `plugins/base.py`；发现/注册在 `plugins/registry.py`。
- **存储** — 单一 MySQL schema `aiops_apm_runtime`，承载全部表：
  - 业务配置 + 输出：`problem_record`、`change_record`、`domain_config`、`monitor_target`、`maintenance_window`、`suppress_blacklist`、`fpr_table`。
  - v2 运行时/历史：`signal_snapshot`、`detection_state`、`detection_round`。
  - 所有表均带 `tenant_id`。`storage_backend` 设置决定用 `mysql` 还是内存实现（仅用于 demo/单测，不引入 SQLite）。
- **配置** — MySQL 为主源（YAML 仅作首次初始化 seed）。`monitor_target` 回答「监控谁、从哪采、多快采」；`domain_config` 回答「怎么判、怎么抑制、怎么验证」。运行时动态配置（维护窗口、黑名单、误报率）每轮重新读取。
- **调度器** — `scheduler.py` 按每个 `monitor_target` 的 schedule 触发（默认 60s 间隔）；`poller.py` 执行单轮。`POST /v1/monitors/{id}/run` 复用同一单端点路径做手动触发。

### 预期目录结构（`src/aiops_apm/`）

`settings.py`、`exceptions.py`、`_app.py`（FastAPI 工厂 + lifespan）· `config/`（loader + seed YAML）· `models/`（signal/anomaly/record + `fingerprint.py`）· `plugins/`（base + registry）· `collectors/`、`detectors/`、`suppressors/`（内置插件）· `pipeline/`（context、runner、l0–l3、emit）· `storage/`（连接池 + stores）· `router/`（API）· `scheduler.py`、`poller.py`。

关键细节 — `models/fingerprint.py` 是 `anomaly_key()`（单个异常稳定指纹）与 `group_key()`（排序无关的 `tenant_id:domain:service:<hash>`）的唯一真源，用于去重和 L3 持续性判断。契约在 M1 冻结，之后只允许增加可选字段。

## 命令

以下命令来自实现计划中各里程碑的完成标准（`Makefile`/`pyproject.toml` 已在 M0 创建）：

- `make lint` — ruff + mypy
- `make test` — pytest（先写测试；计划是 TDD 驱动，§13 用例为测试来源）
- `make dev` — 开发模式启动 uvicorn
- `make migrate` — 建齐单 schema（`aiops_apm_runtime`）所有表
- `docker compose up` — 完整环境（mysql、mock-source、apm-alert、prometheus）
- 用 `uvicorn ...` 启动服务；`GET /health`、`GET /ready` 为探针，`GET /metrics` 暴露 Prometheus 指标

启动/手动调用/配置说明见 [`README.md`](../README.md) 的「启动与快速上手」章节（M0–M7 共用，每完成一个里程碑补充该阶段的启动附加步骤）。

配置使用 `pydantic-settings`，环境变量前缀为 `APM_`（如 `APM_DB_HOST`、`APM_PORT`、`APM_ENABLE_SCHEDULER`）。

## 测试

`apm-alert-module-design.md` §13 的 11 个验收场景是 TDD 来源——例如：CPU 飙高、内存泄漏（组合信号升 critical）、代码 bug（纯日志、count 聚合）、同源关联、变更关联、瞬时抖动（L3 持续性闸门）、维护窗口（L0）、误报率过高（L3）、无信号（零 LLM 调用）、日志源降级（不崩溃）。单测运行使用标准 pytest（`pytest tests/test_pipeline.py::test_name -k ...`）。

## 多租户约定

所有 `/v1/*` 接口在服务端从 `X-Tenant-Id` 请求头解析 `tenant_id`（默认 `default`）——绝不信任请求体中的 `tenant_id`。
