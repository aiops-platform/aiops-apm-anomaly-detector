# SIP UI「APM Monitors」监控端点页 — 实现计划（前端对接 3.2①）

> 状态：**进行中（待审核）**。
> 前端实现仓库：`service-intelligence-platform-ui`（本仓库上一级目录，纯静态站点：`index.html` + `js/app.js` + `css/styles.css`，无框架无构建）。
> 本计划存于后端仓库 `docs/plans/`，便于评审；执行时跨两仓库：后端两处改动（CORS + 滚动时间窗口，见 §8），前端改动全在 SIP UI 仓库。

## 1. Context（为什么做）

- 后端 `aiops-apm-anomaly-detector` M0–M7 已完成（`make lint test dev` 全绿，351 用例），监控端点相关 API **已全部就绪**：`/v1/monitors` CRUD + `/{id}/test`（连通测试）+ `/{id}/run`（手动单跑）+ 调度器自动跑。
- 后端**无任何前端页面**（M0 明确不做前端），图形化配置只能靠前端。用户决定前端在 `service-intelligence-platform-ui` 项目实现。
- 本次范围：**只做监控端点 3.2①（monitor_target）**——列表 + 新建/编辑 + 连通测试 + 手动单跑，**单页面布局**（表单在列表上方，测试/单跑为按钮点击执行，不跳独立详情页）。
- 目标：配好 monitor_target（`enabled=true` + `schedule.interval_sec`）后，后端调度器（`APM_ENABLE_SCHEDULER=true`，默认开）即自动按配置间隔定时跑。
- **已确认追加后端增强（方案 B，滚动时间窗口）**：日志/指标采集支持「每次触发采最近 N 分钟」——`source_config.window_sec`（回看秒数）设了即每轮动态下推 `start=now-window` / `end=now`；未配置时保持既有水位线增量行为不变（向后兼容）。

## 2. 菜单位置（英文）

```
Service Ops
└── Observability（已有分组，英文）
    ├── Smart Inspection（已有）
    ├── Metrics Analysis（已有）
    ├── Log Analysis（已有）
    └── APM Monitors（新增）          ← 本次
```

- Sidebar 子项：**APM Monitors**
- Breadcrumb：`Service Ops › Observability › APM Monitors`
- 页面标题：**APM Monitors**

## 3. 页面布局（单页：顶部表单 + 结果区 + 底部列表）

```
┌───────────────────────────────────────────────────────────────┐
│ Breadcrumb: Service Ops › Observability › APM Monitors         │
├───────────────────────────────────────────────────────────────┤
│ § MONITOR TARGET FORM（顶部表单，新建/编辑复用）                │
│  [Service] [Signal Type▾] [Source Type▾] [Domain] [Enabled ✓]  │
│  [Interval (sec)]                                              │
│  [Source URL] + SSRF 提示（仅 http/https，拦截私网）            │
│  [Rows Path] [Signature Frames]                                │
│  [Field Mapping] JSON 编辑器 + 「模板」下拉一键填入              │
│  [Headers] JSON 编辑器（凭据须用 ${env:X}/${vault:...}）       │
│  [Params] JSON 编辑器                                          │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ 当前操作对象: MT-0001 (Editing)  /  New Target           │   │
│  │ [Save] [Connectivity Test] [Manual Run] [Reset]         │   │
│  └─────────────────────────────────────────────────────────┘   │
├───────────────────────────────────────────────────────────────┤
│ § TEST / RUN RESULT（可折叠，测试与单跑结果在此展示）           │
│  signal_count · signals 样本 / DomainResult / error reason      │
├───────────────────────────────────────────────────────────────┤
│ § MONITOR TARGETS（底部列表）                                  │
│  [Search service] [Filter signal_type] [New]                   │
│  | target_id | service | signal_type | source_type | domain    │
│  | interval | enabled | 操作(Edit/Test/Run/Delete)             │
└───────────────────────────────────────────────────────────────┘
```

## 4. 顶部表单字段（Monitor Target Form）

字段规格严格对齐后端 `POST /v1/monitors` / `PUT /v1/monitors/{id}` 契约（后端实现真源：`storage/monitor_targets.py` + `collectors/__init__.py` 分派矩阵）。

| 表单 Label（英文） | JSON 键 | 类型 | 必填 | 默认 | 说明 / 前端校验 |
|---|---|---|---|---|---|
| Service | `service` | 文本 | ✅ | — | 服务名，如 `order-management`；用于按 service 分组开单 |
| Signal Type | `signal_type` | 下拉 | ✅ | `metric` | `metric` / `log` / `change` |
| Source Type | `source_type` | 下拉 | ✅ | `prometheus` | `prometheus` / `http` / `elk` / `mock`；分派矩阵：metric+prometheus/http→http_metrics；log+http/elk→http_logs；mock→mock（不产信号） |
| Domain | `domain` | 文本 | — | `application` | 决定用哪个 `domain_config` 规则；`application` 空表自动 seed |
| Enabled | `enabled` | 开关 | — | `true` | 关掉则不参与调度（DELETE 即软删=0） |
| Interval (sec) | `schedule.interval_sec` | 数字 | — | `60` | 调度间隔（秒）→ **自动定时跑的节奏** |
| Source URL | `source_config.url` | 文本 | ✅ | — | 仅 http/https；**SSRF 网关拦截私网**（127/8、10/8、172.16/12、192.168/16、169.254/16、::1）→ 前端提示 |
| Rows Path | `source_config.rows_path` | 文本 | — | `data.result` | 响应数组行的点路径（Prometheus instant query 形状） |
| Signature Frames | `source_config.signature_frames` | 数字 | log 时 | `3` | 日志堆栈签名帧数 |
| Field Mapping | `source_config.field_mapping` | JSON 编辑器 | metric/log 必填 | — | 键：`metric`/`value`/`timestamp`/`service`/`level`/`message`/`stack_trace`；支持点路径与 `value[1]` 数组索引 |
| Headers | `source_config.headers` | JSON 编辑器 | — | `{}` | `authorization`/`x-api-key` **必须用 `${env:X}` 或 `${vault:path#key}` 引用**（拒明文凭据） |
| Params | `source_config.params` | JSON 编辑器 | — | `{}` | 额外查询参数（**URL 查询串**；指标采集还会自动下推 `start` 水位线）。⚠️ **不要在这里放日志级别过滤** —— 对 ES 源是死路：自定键会让 ES 返回 400 `unrecognized parameter`，换 `q` 则会整体覆盖 body 的 query、静默丢掉水位线窗口。级别过滤用 `level_field` + `levels`（见下） |
| Time Field / Service Field / Level Field | `source_config.time_field` / `service_field` / `level_field` | 文本 | — | — | **ES（`elk`）源的查询开关**，设任一个即启用：时间窗、服务过滤、级别过滤放进 **POST body** 的 ES 查询 DSL。ES 的日期 range 只认 body，写进 URL 参数会 400；`.keyword` 后缀与大小写都必须精确。`level_field` 的用途是把无意义的量挡在 ES 侧：源端单轮新增量远超 `size`（默认 500）时，水位线一轮只推进几毫秒、积压永久累积，真正的 ERROR 永远轮不到 |
| Levels | `source_config.levels` | 文本（逗号分隔） | — | — | 配合 `level_field` 生成 `terms` filter，如 `ERROR, WARN` → `["ERROR","WARN"]`。取值须与源端**大小写完全一致**（`.keyword` 是精确 term）；留空即不下发该过滤（空 `terms` 匹配 0 条，会让该端点静默采不到日志） |
| Window (sec) | `source_config.window_sec` | 数字 | — | 未设 | **方案 B**：回看窗口（秒）。设了即每轮动态下推 `start=now-window_sec` / `end=now`（固定滚动窗口，覆盖水位线）；未设走水位线增量。**建议 `window_sec >= interval_sec` 防漏** |
| Time Params | `source_config.time_params` | JSON 编辑器 | — | `{"start":"start","end":"end"}` | **方案 B**：窗口参数名映射（源用 `from`/`to` 等时改这里，如 `{"start":"from","end":"to"}`） |
| Timezone | `source_config.timezone` | 文本 | — | — | 源所在时区（IANA，如 `Asia/Shanghai`）。出站时间统一 `yyyy-MM-dd'T'HH:mm:ss.SSS` + 时区后缀（UTC→`Z`）；**Spring 等源按本地墙钟解析查询参数（忽略时区后缀）**，水位线是 UTC，配了 `timezone` 才转源时区再发，否则漂移 8 小时重复采集 |

**Field Mapping 模板（「Template」下拉一键填入，降低误配）：**
- Prometheus 形状：`{"metric": "metric.__name__", "value": "value[1]", "timestamp": "value[0]", "service": "service"}`
- 行即字段：`{"metric": "metric", "value": "value", "timestamp": "timestamp", "service": "service"}`

## 5. 交互逻辑

| 动作 | 触发 | 行为 |
|---|---|---|
| New | 顶部按钮 / 列表「New」 | 清空表单，操作对象=New Target，Save 走 `POST` |
| Edit | 列表行「Edit」或点行 | 该行数据回填顶部表单，操作对象=target_id（Editing），Save 走 `PUT` |
| Save | 表单按钮 | 校验必填 → `POST /v1/monitors`（201 返回 target_id）或 `PUT /v1/monitors/{id}` → 刷新列表 + toast 成功 |
| Connectivity Test | 表单按钮「Connectivity Test」 | 需已加载 target_id → `POST /v1/monitors/{id}/test` → 结果写入 TEST/RUN RESULT 区（signal_count + 前 20 条信号样本） |
| Manual Run | 表单按钮「Manual Run」 | 需已加载 target_id → `POST /v1/monitors/{id}/run` → 结果写入结果区（anomaly_count / records / suppressed_count / degraded_sources / timeline 摘要） |
| Delete | 列表行「Delete」 | confirm → `DELETE /v1/monitors/{id}`（204 软删）→ 刷新列表 |
| Reset | 表单按钮 | 清空表单恢复 New 状态 |

> 提示文案：Manual Run **第 1 轮通常不开单**（L3 持续性 `persistence_rounds` 默认 2，同一异常连续 2 轮命中第 2 轮才落 `problem_record`）——结果区加一行说明「再点一次 Run 看是否落单」。

## 6. 后端 API 对接总表（3.2① 用到的全部端点）

| 目的 | 方法+路径 | 请求 | 成功返回 | 失败（统一 `{code,reason,trace_id}`） |
|---|---|---|---|---|
| 新建 | `POST /v1/monitors` | §4 body | 201 `{"target_id":"MT-0001"}` | 400（SSRF / 字段） |
| 列表 | `GET /v1/monitors` | `?service=&signal_type=` | `{"items":[...]}` | — |
| 详情 | `GET /v1/monitors/{id}` | — | 端点 dict | 404 `NOT_FOUND` |
| 编辑 | `PUT /v1/monitors/{id}` | 部分字段 patch | 更新后 dict | 404 / 400（改 url/headers 会重新过网关） |
| 删除 | `DELETE /v1/monitors/{id}` | — | 204（软删） | 404 |
| 连通测试 | `POST /v1/monitors/{id}/test` | — | `{"status":"ok"\|"error","signal_count":N,"signals":[...]}` | 404；网关错误 400；上游失败 status=error（200） |
| 手动单跑 | `POST /v1/monitors/{id}/run` | — | `{"domain","records":[],"suppressed_count":N,"anomaly_count":N,"degraded_sources":[],"timeline":...}` | 404 |

**统一请求头（每个请求必带）：** `X-Tenant-Id: default`（默认租户，前端常量/可配置）。**鉴权：** 后端 `APM_API_KEYS` 未配置时全放行；配置后需 `Authorization: Bearer <key>`（前端 localStorage 存 key，可配置）。

## 7. 「配好即自动定时跑」前置条件（前端页面加引导文案）

配置完 monitor_target 后，调度器自动跑需满足：
1. **domain_config 必须存在**：空表时后端只自动 seed `application` 域 → **首期用 `domain: application` 即可免配规则**；若用自定义 domain，需先 `PUT /v1/config/{domain}` 建规则（属 3.3②，不在本期）。
2. **`APM_ENABLE_SCHEDULER=true`**（默认 true，`.env` 勿关）；关闭则只手动跑。
3. 端点 **`enabled=true`** 且填 **`schedule.interval_sec`**（如 60=每 60 秒一轮；首个周期等一个 interval 才触发，防启动风暴）。
4. **要看到真实告警**还需：真实 HTTP 源（`http_metrics`/`http_logs`，**API 建的 mock 端点恒 0 信号**）+ 信号值越过检测阈值 + 同一异常连续 2 轮（`persistence_rounds=2`）→ 第 2 轮才落 `problem_record`。

## 8. 后端改动（本仓库，两处）

### 8.1 CORS（前端跨端口调用前置）

**现状：** 后端 `_app.py` **无 CORS 中间件**。SIP UI 静态页若跑在 `:8080`，浏览器 `fetch` 到后端 `:7070` 会被 CORS 拦截。

两个解法二选一：
- **推荐（改本仓库约 10 行 + 单测）**：`create_app` 加 `CORSMiddleware`，`allow_origins` 含 UI 来源（如 `http://localhost:8080`）、`allow_methods=[GET,POST,PUT,DELETE,OPTIONS]`、`allow_headers` 含 `X-Tenant-Id, Authorization, Content-Type`。
- **不改后端**：SIP UI 起同源代理把 `/v1/*` 转发到后端。

> 执行时按**推荐方式**实施：CORS 为可配置项（`APM_ALLOWED_ORIGINS`，默认空=不挂中间件），保持现有行为不变，不破坏 351 用例。

### 8.2 滚动时间窗口（方案 B，用户已确认）

**需求**：定时采集时按「trigger 时间 - N 分钟 → trigger 时间」下推窗口，满足「每次触发采最近 N 分钟」的精确诉求（如每 3/5 分钟跑一次、采最近 3/5 分钟 error 日志）。

**现状**（读代码确认）：`http_metrics.py:35-39` / `http_logs.py:37-42` 只在有水位线时下推 `params["start"]=last_ts`，**不推 `end`**，也不按 now 算窗口；`CollectContext`（`collectors/_context.py`）只有 `tenant_id`/`watermark_store`/`snapshot_store`，**无 `now`**。

**改动设计**：
- `collectors/_context.py`：`CollectContext` 加可选字段 `now: datetime | None = None`（向后兼容；缺省时采集器回退 `datetime.now(timezone.utc)`）。
- 新增共享助手 `collectors/_window.py`：`apply_time_window(sc, ctx, params) -> params`——当 `sc.window_sec > 0` 时：
  ```
  now = ctx.now or datetime.now(timezone.utc)
  tp = sc.get("time_params", {})                      # 参数名映射，默认 {"start":"start","end":"end"}
  params[tp.get("start", "start")] = (now - timedelta(seconds=window)).isoformat()
  params[tp.get("end", "end")] = now.isoformat()
  ```
  `window_sec` 设了 → 走固定窗口（**覆盖水位线 start**）；未设 → 保持既有水位线增量逻辑不变。
- `http_metrics.py` / `http_logs.py`：把现有「水位线下推」块替换为 `apply_time_window(...)`（未设窗口时行为与现在一致，既有测试不破）。
- `poller.py`：`CollectContext(..., now=now)` 注入本轮 trigger 时间（确定性，同 `detection_round.started_at`）。
- `router/monitors.py` `/test` 不改（ctx.now 缺省回退当前时间即可）。

**文件清单**：
```
src/aiops_apm/collectors/_context.py     # 改：CollectContext 加 now 可选字段
src/aiops_apm/collectors/_window.py      # 新增：apply_time_window 共享助手
src/aiops_apm/collectors/http_metrics.py # 改：用 apply_time_window
src/aiops_apm/collectors/http_logs.py    # 改：用 apply_time_window
src/aiops_apm/poller.py                  # 改：CollectContext 注入 now
tests/test_collectors.py                 # 改：加窗口用例
```

**测试要点（TDD，先写测试）**：
| 用例 | 断言 |
|---|---|
| `window_sec` 设 + `ctx.now` 注入 | params 含 `start=now-window`、`end=now`；**不使用**水位线 |
| `time_params` 自定义（`{"start":"from","end":"to"}`） | 下推 `from`/`to` 参数名 |
| `window_sec` 未设 | 与现状一致：有水位线→下推 `start=last_ts`；无水位线→不下推 |
| `window_sec <= 0` | 视为未设（不报错） |

**设计注意**：
- **建议 `window_sec >= interval_sec`**：等于=连续无重叠；大于=重叠靠幂等去重兜底；小于=可能漏采日志。
- ⚠️ **级别过滤不要用 `params`**（本文档早期版本此处写的是 `params: {"level":"error"}`，实测是死路：ES 400 `unrecognized parameter`；换 `q` 会整体覆盖 body 的 query、静默丢掉窗口）。走 `source_config.level_field` + `levels`，与窗口下推天然叠加（前者进 body，后者进 URL 查询串，互不干扰）。详见后端 `docs/logs/M9.md` 的「日志 target 按级别过滤」一节。
- 时间格式：ISO8601 字符串（与现有水位线 `isoformat()` 一致）。

## 9. 前端改动文件清单（SIP UI 仓库，遵循其约定）

```
service-intelligence-platform-ui/
├── index.html                    # 改：Observability 分组下加 sidebar 子项 <div data-page="apm-monitors">APM Monitors</div>；
│                                 #     新增本页 DOM（顶部表单区 + 结果区 + 列表区），复用现有 page/breadcrumb 样式
├── js/app.js                     # 改：加 const APM_BASE_URL（可配置）+ 统一 api() fetch 封装（带 X-Tenant-Id/Bearer）；
│                                 #     按既有模式加 initApmMonitors()（列表渲染/筛选/行点击回填/删除）+
│                                 #     initApmMonitorForm()（表单提交/模板下拉/Connectivity Test/Manual Run/Reset）；
│                                 #     在主 init() 中调用
├── css/styles.css                # 改：复用现有工具类（.kpi-card/.filter-chip 等），表单按现有组件风格补少量样式
├── changelogs/v1.3.0-apm-monitors.md   # 改：**强制**（当前 v1.2.1，功能新增升 minor），模板见 CONTRIBUTING.md 第 48–90 行
└── README.md                     # 改（可选）：加 APM Monitors 页面说明
```

## 10. 验收清单（联调端到端）

| # | 操作 | 预期 |
|---|---|---|
| 1 | 打开菜单 **APM Monitors** | 顶部表单空态 + 列表空态（或已有端点），Breadcrumb 正确 |
| 2 | 新建 `signal_type=metric / source_type=mock / url=http://8.8.8.8/metrics` → Save | 201 返回 target_id，列表出现该行 |
| 3 | 新建 `url=http://169.254.169.254/...` → Save | toast「blocked network」（SSRF 生效，400） |
| 4 | 列表行点 Edit | 表单回填该行，Save 变 PUT |
| 5 | 选中端点点 Connectivity Test | 结果区显示 signal_count 与信号样本 |
| 6 | 配真实 Prometheus 形状源 + 超阈值，点两次 Manual Run | 第 2 次结果区出现 record_id |
| 6b | 配 `window_sec=180` + `interval_sec=180`，源返回 error 日志，点 Manual Run | 结果区 signals_count>0；源侧收到 `start≈now-180s`、`end≈now`（可从 signals 时间戳反推） |
| 7 | 端点 enabled + interval_sec=60，后端 `APM_ENABLE_SCHEDULER=true` | 等一个周期后 `GET /v1/audit/rounds` 出现自动轮次 |
| 8 | `make lint` / `make test`（后端 CORS + 滚动窗口改动后） | 全绿（351 用例 + 新增窗口用例，无回归） |

## 11. 执行顺序（审核通过后）

1. **后端 CORS（§8.1，本仓库）**：`settings.py` 加 `APM_ALLOWED_ORIGINS`（空=不挂）；`_app.py` 条件挂 `CORSMiddleware`；补 `test_cors.py`。`make lint test` 全绿。
2. **后端滚动窗口（方案 B，§8.2，本仓库）**：`CollectContext.now` + 新增 `_window.py` + 两采集器替换 + `poller.py` 注入 now + `test_collectors.py` 加窗口用例。`make lint test` 全绿。
3. **前端 SIP UI**：按 §3/§4/§5/§9 实现（表单含 Window (sec) / Time Params 两字段；index.html + js/app.js + css + changelog）。
4. **联调**：按 §10 验收清单逐项过（含 6b 窗口用例）。
