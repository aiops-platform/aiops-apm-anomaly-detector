# SIP UI「APM Domain Rules」域规则可视化配置页 — 实现计划

> 状态：**进行中（待审核）**。
> 前端实现仓库：`service-intelligence-platform-ui`（纯静态站点：`index.html` + `js/app.js` + `css/styles.css`，无框架无构建）。
> 本计划存于后端仓库 `docs/plans/`，便于评审（对齐 `docs/plans/UI-monitors-implementation-plan.md` 约定）；执行时跨两仓库：后端一处改动（新增 `GET /v1/config` 列表接口，见 §8），前端改动全在 SIP UI 仓库。

## 1. Context（为什么做）

- `domain_config` 是每域的检测规则（`detectors` / `suppressors` / `correlation` / `verify`），存 MySQL 的 JSON 列。现状改规则只能 `PUT /v1/config/{domain}` 传**整个 JSON**，或直接改表——不直观、易错。
- 需求：在前端页面里**按字段表单配置**规则（每个 detector 一张卡、params 用键值对行编辑），而不是编辑一整块 JSON。
- 现状调研结论（读代码确认）：
  - 后端 `GET /v1/config/{domain}`（返回 `{domain, config, version}`，结构化 config）已就绪。
  - 后端 `PUT /v1/config/{domain}`（`DomainConfig.model_validate` + `validate_domain_config` 表驱动校验 → 400 带 reason）已就绪。
  - 后端 `GET /v1/plugins`（返回 `{collectors, detectors, suppressors}` 插件名）已就绪。
  - 后端**缺**「列出所有域」的接口 → 前端域下拉需要 → 本次补 `GET /v1/config`。
- 用户已确认两个取舍：**params 用键值对编辑行**（值按 JSON 解析）；**入口为 APM 组下新增侧边栏项 Domain Rules**。
- 前端 `initMonitorTargets`（monitor-targets 页，表单化 CRUD + 局部 `apmApi` 封装）是本页实现模板，完全照搬。

## 2. 菜单位置（英文）

```
Service Ops
└── Observability（已有分组，英文）
    ├── Smart Inspection（已有）
    ├── Metrics Analysis（已有）
    ├── Log Analysis（已有）
    ├── APM Monitors（已有，monitor-targets 页）
    └── APM Domain Rules（新增）          ← 本次
```

- Sidebar 子项：**Domain Rules**
- Breadcrumb：`Service Ops › Observability › APM › Domain Rules`
- 页面标题：**Domain Rules**

## 3. 页面布局（单页：域选择 + 规则卡片）

```
┌───────────────────────────────────────────────────────────────┐
│ Breadcrumb: Service Ops › Observability › APM › Domain Rules   │
├───────────────────────────────────────────────────────────────┤
│ 顶部工具栏                                                     │
│  [Domain ▾  application]  [Load]  [Save]  ── dcStatus 提示区   │
├───────────────────────────────────────────────────────────────┤
│ § DETECTORS（卡片列表，容器 dcDetectors）                       │
│  ┌─ Detector 1 ────────────────────────────────────────────┐   │
│  │ Signal [cpu_usage      ]  Plugin [static_threshold ▾]   │   │
│  │ Severity [warning ▾]                         [Delete]   │   │
│  │ Params:                                                  │   │
│  │   [threshold        ] = [0.9             ]  [×]         │   │
│  │   [window_sec       ] = [60              ]  [×]         │   │
│  │   [+ Add Param]                                          │   │
│  └──────────────────────────────────────────────────────────┘   │
│  [+ Add Detector]                                               │
├───────────────────────────────────────────────────────────────┤
│ § SUPPRESSORS（卡片列表，容器 dcSuppressors）                   │
│  ┌─ Suppressor 1 ──────────────────────────────────────────┐   │
│  │ Name [maintenance_window ▾]                  [Delete]   │   │
│  │ Params:  [duration_minutes] = [60]  [×]  [+ Add Param]  │   │
│  └──────────────────────────────────────────────────────────┘   │
│  [+ Add Suppressor]                                             │
├───────────────────────────────────────────────────────────────┤
│ § CORRELATION / VERIFY（静态输入，滚动区）                      │
│  Metric-Log Window (sec) [300]  Change Window (sec) [300]      │
│  Persistence Rounds [2]  FP Threshold [0.6]  Min Samples [20]  │
└───────────────────────────────────────────────────────────────┘
```

## 4. 表单字段（对齐 `DomainConfig` schema，真源：`src/aiops_apm/models/config.py`）

| 区块 | 字段 | JSON 键 | 类型 | 默认 | 说明 / 前端规则 |
|---|---|---|---|---|---|
| Detectors | Signal matcher | `detectors[].signal` | 文本 | — | 普通字符串=信号名；trimmed 以 `{` 开头 → `JSON.parse` 成结构化 matcher（如 `{"metric":"cpu_usage"}`） |
| Detectors | Plugin | `detectors[].plugin` | 下拉 | — | 数据源 `GET /v1/plugins.detectors`（`static_threshold`/`simple_compare`/`signature_aggregate`）；拉取失败回退文本输入 |
| Detectors | Severity | `detectors[].severity` | 下拉 | `warning` | `warning` / `high` / `critical`（L1 检测以 spec.severity 权威覆盖） |
| Detectors | Params | `detectors[].params` | 键值对行 | `{}` | 每行「参数名 + 值」；值 JSON 解析（见 §5 规则）；空参数名跳过 |
| Detectors | 删除卡片 | — | 按钮 | — | confirm 后移除该 detector |
| Suppressors | Name | `suppressors[].name` | 下拉 | — | 数据源 `GET /v1/plugins.suppressors`（`maintenance_window`/`blacklist`）；失败回退文本输入 |
| Suppressors | Params | `suppressors[].params` | 键值对行 | `{}` | 同 detectors |
| Suppressors | 删除卡片 | — | 按钮 | — | confirm 后移除该 suppressor |
| Correlation | Metric-Log Window (sec) | `correlation.metric_log_window_sec` | 数字 | `300` | L2 指标+日志同源关联窗口 |
| Correlation | Change Window (sec) | `correlation.change_window_sec` | 数字 | `300` | L2 变更关联窗口 |
| Verify | Persistence Rounds | `verify.persistence_rounds` | 数字 | `2` | L3 持续性：同一异常连续 N 轮才开单 |
| Verify | False Positive Threshold | `verify.false_positive_threshold` | 数字(0~1) | `0.6` | L3 误报率闸门 |
| Verify | Min Samples | `verify.min_samples` | 数字 | `20` | L3 最小样本数 |

## 5. 交互逻辑

| 动作 | 触发 | 行为 |
|---|---|---|
| 页面打开 | sidebar 切到 Domain Rules | `refresh()`：`GET /v1/config` 填 `dcDomainSelect`（保留当前选中）→ 自动 `loadDomain(当前域)` |
| 选域 | `dcDomainSelect` change | `loadDomain(domain)` → `GET /v1/config/{domain}` → 渲染卡片 + 回填 correlation/verify |
| Load | 顶部按钮 | 重新加载当前域（丢弃未保存改动） |
| 添加 Detector / Suppressor | 区块底部按钮 | 追加一张空卡片（plugin/name 下拉或文本输入） |
| 删除 Detector / Suppressor | 卡片 Delete | confirm → 移除卡片 |
| 添加 / 删除参数行 | `+ Add Param` / `[×]` | 键值对行增删 |
| 保存 | 顶部 Save | `collect()` 组装 `DomainConfig` → `PUT /v1/config/{domain}` → 成功（显示返回 version）/ 失败（展示后端 400 reason）→ `dcStatus` |
| 插件下拉拉取失败 | `GET /v1/plugins` 非 2xx | 回退文本输入框（不阻塞编辑） |

**params 值 JSON 解析规则（`collect()` 时逐行）**：`JSON.parse(value)` 成功用其结果（`true`→bool、`0.9`→number、`"abc"`→string），失败回退原始字符串；空参数名跳过。

> 提示文案：Save 走 admin 接口，未配置后端 `APM_API_KEYS` 时返回 401/403——`dcStatus` 显示后端 reason 引导配置。

## 6. 后端 API 对接总表

| 目的 | 方法+路径 | 请求 | 成功返回 | 失败（统一 `{code,reason,trace_id}`） |
|---|---|---|---|---|
| 域列表（**新增**） | `GET /v1/config` | — | `{"items":[{"domain","enabled","version"}]}` | — |
| 域详情 | `GET /v1/config/{domain}` | — | `{"domain","config","version"}` | 404 `NOT_FOUND` |
| 保存规则 | `PUT /v1/config/{domain}` | `DomainConfig` body | `{"domain","version"}` | 400（结构校验 reason）/ 401/403（非 admin） |
| 插件列表 | `GET /v1/plugins` | — | `{"collectors","detectors","suppressors"}` | — |

**统一请求头：** `X-Tenant-Id: default`（localStorage `apmTenantId`）。**鉴权：** 后端 `APM_API_KEYS` 未配置 → 全放行（GET/PUT 均可）；配置后 → GET 可带 Bearer、PUT 需 admin scope key（localStorage `apmApiKey`）。

## 7. 前端改动文件清单（SIP UI 仓库，遵循其约定）

```
service-intelligence-platform-ui/
├── index.html        # 改：APM 组（#apm children）monitor-targets 之后加 sidebar 子项
│                     #     <div class="sidebar-item" data-page="domain-config">Domain Rules</div>；
│                     #     新增 page-domain-config 面板（breadcrumb + 域下拉 + Load/Save + dcStatus + 主体容器）
├── js/app.js         # 改：新增 initDomainConfig() IIFE 模块（局部 apmApi 封装 + refresh/loadDomain/
│                     #     卡片渲染/键值对行/collect/save）；侧边栏 handler 加 else if 分支；
│                     #     主 init() 调用 initDomainConfig()
├── css/styles.css    # 改：优先复用 .btn/.reflow-table 等；仅补少量 .rule-card/.kv-row 布局类（BEM 风格）
└── changelogs/v1.5.0-domain-rules.md  # 改：强制（当前 v1.4.9，功能新增升 minor），模板见 CONTRIBUTING.md
```

**`js/app.js` 实现细节（模式同 `initMonitorTargets`）：**
- 局部 `apmApi(path, opts)`：`APM_BASE_URL`/`TENANT_ID`/`API_KEY` 从 localStorage（`apmBaseUrl`/`apmTenantId`/`apmApiKey`）读，加 `X-Tenant-Id` + 可选 Bearer，非 2xx 提取后端 `{reason}` 抛给调用方展示。
- `refresh()` 注册到 `_pageRefreshers['page-domain-config']`。
- `loadDomain(domain)`：`GET /v1/config/{domain}` → 卡片渲染 + 静态区回填；并行 `GET /v1/plugins` 供下拉（失败回退文本输入）。
- `collect()`：逐卡片读 signal/plugin/severity + 键值对行（JSON 解析）→ 组装 `{detectors, suppressors, correlation, verify}` → `save()` PUT。
- 保存成功后刷新域下拉（version +1 可见）。

## 8. 后端改动（本仓库，一处）

### 8.1 新增 `GET /v1/config` 域列表接口（供前端域下拉）

`src/aiops_apm/router/config.py` 新增：

```python
@router.get("")
async def list_domain_configs(request: Request) -> dict:
    tenant = get_tenant_id(request)
    rows = await DomainConfigLoader(_storage(request).domain_configs).load(tenant)
    return {"items": [
        {"domain": r["domain"], "enabled": r["enabled"], "version": r["version"]} for r in rows
    ]}
```

- 复用现有 `DomainConfigLoader.load`（`config/loader.py`：空表自动 seed `application`、DB 故障回退 last-known-good）——返回行即 `{"domain","config","enabled","version"}`（loader.py:3 注释 + `DomainConfigStore.load` 同形）。
- 路由注册：`@router.get("")` 是精确路径 `/v1/config`（无路径段），与既有 `@router.get("/{domain}")` 不冲突（FastAPI 路径参数要求非空段）。
- 读接口，无需 admin（同 `GET /v1/config/{domain}`）。

**测试（TDD）**：`tests/test_config_api.py` 新增 `test_list_domain_configs`：`GET /v1/config` → 200，`items` 非空且含 `domain=application`，每项含 `enabled`/`version` 键。

## 9. 前置条件（联调时需满足）

1. 后端 `make dev` 运行（`http://localhost:7070`），前端静态服务 `python3 -m http.server 8080`。
2. CORS：`.env` 里 `APM_ALLOWED_ORIGINS='["*"]'`（**必须单引号包裹**——双引号会被 `set -a; . ./.env` 吃掉、`*` 被 glob 展开成 `[[*]]` → SettingsError；否则前端带 `X-Tenant-Id` 的 fetch 预检 405）。
3. 保存需 admin：后端配置 `APM_API_KEYS`（Bearer key）+ 前端 localStorage 设 `apmApiKey`；未配时 GET 可读、PUT 返回 401/403。
4. 本页无 URL 输入（规则配置不含数据源 URL），不受 SSRF 网关影响。

## 10. 验收清单（联调端到端）

| # | 操作 | 预期 |
|---|---|---|
| 1 | 打开 APM › Domain Rules | 域下拉列出 `application`（+ 已建域），自动加载第一个域 |
| 2 | 默认域表单 | detectors/suppressors 卡片回填 + correlation/verify 数值正确（对齐 `config/domains.yaml` seed） |
| 3 | 加 detector（signal=`cpu_usage`, plugin=`static_threshold`, severity=`warning`, params 键值对 `threshold=0.9`）→ Save | PUT 200 返回 version；后端 `GET /v1/config/application` 确认 version +1 且配置生效 |
| 4 | 参数值输入 `true` / `0.9` / `abc` → Save | 后端 `config.params` 分别为 bool `true` / number `0.9` / string `"abc"` |
| 5 | 错误配置（如 `threshold` 非数字）→ Save | 400 且 `dcStatus` 展示后端 reason（`validate_domain_config`） |
| 6 | 未配 `APM_API_KEYS` 时 | GET 可读、PUT 提示需 admin（401/403 reason） |
| 7 | `make lint test`（后端新增 `test_list_domain_configs` 后） | 全绿，无回归 |
| 8 | `node --check js/app.js` | 无语法错误 |
| 9 | 保存后手动跑一轮（`POST /v1/monitors/{id}/run`） | 新规则生效（如阈值变更影响 anomaly 判定） |

## 11. 执行顺序（审核通过后）

1. **后端（本仓库）**：`router/config.py` 加 `GET /v1/config`（§8.1）+ `tests/test_config_api.py` 加 `test_list_domain_configs`。`make lint test` 全绿。
2. **前端（SIP UI 仓库）**：按 §3/§4/§5/§7 实现（index.html + js/app.js + css/styles.css + `changelogs/v1.5.0-domain-rules.md`）。`node --check js/app.js`。
3. **联调**：按 §10 验收清单逐项过；视觉验证由用户自测。
