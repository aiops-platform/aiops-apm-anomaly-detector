# UI Problem Center（问题中心）页面实现计划

**日期**: 2026-08-28

## Context / 需求

SIP 前端在 **Observability** 下新增一级菜单，集中展示 APM 后端 `problem_record` 表检测出的**全部问题**——不再按日志/指标/内存分类，风格对齐现有「Log Analysis / Error Log Anomalies」页面，并带操作按钮（**分析 / 忽略**）。

**菜单名（用户委托起名）：Problem Center（问题中心）** — 直接挂 Observability 下，与 Smart Inspection / Metrics Analysis / Log Analysis 平级。

**范围：纯前端改动（`service-intelligence-platform-ui`），后端零改动** —— 后端 `/v1/problems` 已具备列表/详情/resolve 能力。

## 数据源（后端接口现状）

| 接口 | 说明 |
|---|---|
| `GET /v1/problems?state=&service=&severity=&limit=` | `{"items":[...]}`，detected_at 倒序，limit 默认 50（页面用 500 客户端聚合） |
| `GET /v1/problems/{id}` | 单条详情 |
| `POST /v1/problems/{id}/resolve` body `{"false_positive": true}` | 关闭 + 误报回写 |

`problem_record` 字段（`models/record.py` + `storage/records.py` 返回 dict）：`record_id / domain / state(pending·in_progress·resolved·closed·archived) / service / severity / detected_at / first_seen_at / last_seen_at / occurrence_count / symptom{summary} / metric_anomalies / log_anomalies / correlation / verification / evidence / trace_id`。

> **`detection_type`（派生字段，接口已返回）**：`log` / `metric` / `combined` / `unknown`，由 `metric_anomalies`/`log_anomalies` 是否为空推出（见 `router/problems.py` 的 `_detection_type`）。**不入库**，列表与详情都带。做「证据类型」筛选芯片直接用这个，不用读 `correlation.reason`。
>
> 注意 `problem_record.source` 是模块名（固定 `apm-alert`），**不是**检测来源，别拿它做类型判断。

## 前端复用点（调研结论）

- 导航：`index.html:96-118` 侧栏 `#observability`；点击处理 `app.js:78-108`（`data-page` → `showPage('page-...')`）。
- 页面切换：`app.js:42` `showPage()` + `_pageRefreshers` 切回自动刷新。
- 页面样板：Log Analysis `index.html:1082-1161`（面包屑/页头+Refresh/5 KPI/工具栏/表格/分页）+ `app.js:3390` `initLogAnalysis()`。
- APM 连接层：`app.js:3666` `initMonitorTargets` 内 `apmApi()`（`APM_BASE_URL`=localStorage `apmBaseUrl` || `http://localhost:7070`，`X-Tenant-Id` 头，可选 Bearer）。
- 复用组件：`confirmModalOverlay`（`index.html:1421`，`app.js:3718` confirmDialog）、`alertDetailOverlay`（`index.html:1460`）、`_showToast`。
- 样式类全部已有（reflow-stats / kpi-card / reflow-filter-bar / filter-chip / reflow-table / pagination / alert-tag / si-refresh-btn / ap-modal-overlay），预计 CSS 零新增。

## 页面设计

- **面包屑**：`Service Ops › Observability › Problem Center`
- **页头**：标题「Problem Center」+ 副标题「Detected Problems」+ Refresh 按钮 + 服务过滤标签
- **KPI 行（5 卡，镜像 Log Analysis）**：Total Problems / Unique Services / Critical / Open(pending+in_progress) / Resolved —— 一次 `limit=500` 拉取后客户端聚合（后端无 summary 端点）
- **工具栏**：服务下拉 + Severity 芯片（All/Warning/High/Critical）+ State 芯片（All/Open/In Progress/Resolved/Closed）
- **表格列**：Problem(标题=`symptom.summary` 截断) / Service / Domain / Severity(标签) / State(标签) / Occurrences(次数徽标) / First Seen(`detected_at`) / Actions
- **分页**：客户端 10/页，复用 pagination 样式

## 操作按钮

- **分析**：打开 `alertDetailOverlay` 弹窗展示完整记录（symptom 摘要、metric/log anomalies、correlation、verification、evidence、trace_id），复用 `app.js:2230` `openAlertDetailGeneric` 渲染思路。
- **忽略**：`confirmDialog` 确认 → `POST /v1/problems/{id}/resolve` body `{"false_positive": true}` → toast + 刷新列表（该单变 Resolved）。

## 文件改动清单

| 文件 | 改动 |
|---|---|
| `service-intelligence-platform-ui/index.html` | ① `#observability`（~line 105）加 `<div class="sidebar-item" data-page="problems">Problem Center</div>`；② 新增 `#page-problems` 面板（仿 `#page-log-analysis`），含 5 KPI、工具栏、表格（分析/忽略 操作列）、分页 |
| `service-intelligence-platform-ui/js/app.js` | ① `initSidebar` 导航加 `problems → showPage('page-problems')`；② 新增 `initProblems()`（复用 `apmApi` 连接 + Log Analysis 渲染），注册 `_pageRefreshers['page-problems']`；③ `init()` 调用 `initProblems()` |
| `service-intelligence-platform-ui/css/styles.css` | 复用现有类，预期无改动；操作按钮复用 `.btn-alert-detail` |
| `service-intelligence-platform-ui/changelogs/v1.7.0-problem-center.md` | 按 CONTRIBUTING 模板（版本/日期/改动明细/影响范围表） |
| 本文件 `docs/plans/UI-problem-center-plan.md` | 实现计划落档 |

## 验证

```bash
cd /Users/h.a.hu/accenture/accenture_aiops_platform/acc-aiops-platform-zjb/service-intelligence-platform-ui
node --check js/app.js            # 语法检查
python3 -m http.server 8080       # 起静态站
```

1. 浏览器 `http://localhost:8080` → 侧栏 Observability → Problem Center（后端 `make dev` 需在跑，APM_PORT 7070）。
2. 页面渲染：KPI 数字、表格行（来自 `/v1/problems`）、服务下拉/严重度/状态筛选、分页切换。
3. 「分析」→ 详情弹窗展示完整问题；「忽略」→ 确认弹窗 → resolve 成功 toast + 列表刷新。
4. 后端未启动时页面降级：空态/错误提示，不白屏。
5. chrome-devtools MCP 端到端点一遍（可选）。
