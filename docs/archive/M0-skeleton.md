# M0 工程基座（已实现，归档）

> 归档自 `docs/apm-alert-implementation-plan-enhanced.md` 的 M0 小节。M0 已完成（见 `docs/logs/M0.md`），此文档为历史规格留存，原实现计划中 M0 小节已标记「已实现」。

## 基础信息

- **目标**：可构建、可起服务、可运行空壳
- **依赖**：无
- **功能点**：`pyproject.toml`（依赖锁定 + 三个 entry_points group 声明占位）；`ruff` + `mypy` + `pytest` + `pre-commit`；`Settings`；`AppException` + `ErrorCode`；`create_app(settings)` + `lifespan`；`/health` `/ready`
- **产出**：`uvicorn` 可启动并响应探针
- **完成标准**：`make lint test dev` 三件套全绿

## 前端菜单与页面（本次未实现，前端技术栈待定后另行补充）

| 菜单路径 | 页面 | 组件 | 说明 |
|---------|------|------|------|
| 系统 > 健康状态 | HealthPage | `HealthStatus` 卡片 | 调用 `GET /health` 展示存活状态；调用 `GET /ready` 展示就绪状态（DB 连接、插件加载） |
| 全局 > 错误页 | ErrorPage | `AppError` 组件 | 捕获前端请求错误，展示 `{code, reason, trace_id}` 结构化错误信息 |

> M0 阶段前端为最简骨架：一个 SPA 壳（路由 + Layout + 上述两个页面），后续阶段往里加菜单。

## 后端实现骨架（M0 已实现）

- `src/aiops_apm/settings.py` — `Settings(BaseSettings)`，`env_prefix = "APM_"`（pydantic v2 用 `SettingsConfigDict`）。字段：`db_host/db_port/db_user/db_password/db_name`、`host/port`、`scheduler_tick_sec/max_concurrent_rounds/total_timeout_sec`、`outbound_timeout_sec/outbound_max_body_bytes`、`enable_llm_summary/enable_scheduler/storage_backend`
- `src/aiops_apm/exceptions.py` — `ErrorCode(str, Enum)` 七个值：`INTERNAL_ERROR` / `NOT_FOUND` / `VALIDATION_ERROR` / `PERMISSION_DENIED` / `PLUGIN_NOT_FOUND` / `CONFIG_ERROR` / `UPSTREAM_TIMEOUT`；`AppException(code, reason, trace_id=None)`
- `src/aiops_apm/_app.py` — `create_app(settings=None)` + `lifespan`；统一异常处理（`AppException` / Starlette `HTTPException` / 兜底 `Exception`）均返回 `{code, reason, trace_id}`
- `src/aiops_apm/router/api.py` — `api_router`：`GET /health` → 200 `{"status":"ok"}`；`GET /ready` → 未就绪 503 `{code: NOT_READY, reason: str(checks)}`

## Use Case（M0 已验收）

- **UC-0.1 系统启动健康检查**：`/health` 恒 200；`/ready` 在 DB 未连接时返回 503 + `{code, reason}`
- **UC-0.2 环境变量覆盖配置**：`APM_PORT=9090` → `settings.port == 9090`
- **UC-0.3 异常标准化响应**：所有异常返回统一 JSON 结构，包含 `code/reason/trace_id`

## 测试

`tests/test_health.py`（UC-0.1）、`tests/test_settings.py`（UC-0.2）、`tests/test_exceptions.py`（UC-0.3），共 8 个用例全部通过。
