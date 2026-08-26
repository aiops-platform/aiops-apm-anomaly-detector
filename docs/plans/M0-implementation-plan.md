# M0 工程基座 — 实现计划

## Context（为什么做）

仓库目前只有设计文档（`docs/`、`README.md`、`LICENSE`、`.gitignore`），没有任何 Python 源码。M0 是 `docs/apm-alert-implementation-plan-enhanced.md` 中定义的第一个里程碑，目标**「可构建、可起服务、可运行空壳」**，完成标准为 **`make lint test dev` 三件套全绿**。它是后续 M1–M7 的地基（横切层：进程能起、配置能加载、异常标准化、探针可用），不属于业务流程任何一步。

**用户已确认的决策**：
- 不做前端（设计文档无前端技术栈定义，M0 完成标准也不含前端；前端等后续定栈再补）
- 工具链用 **pip + venv**（pyproject.toml + `pip install -e ".[dev]"`，requirements.txt 锁版本）

## 范围

### M0 交付（3 个 Use Case）
| UC | 名称 | 断言 |
|----|------|------|
| UC-0.1 | 系统启动健康检查 | `/health` 恒 200 `{"status":"ok"}`；`/ready` 无 DB 时 503 + `{code, reason}` |
| UC-0.2 | 环境变量覆盖配置 | `APM_PORT=9090` → `settings.port == 9090` |
| UC-0.3 | 异常标准化响应 | 所有异常统一返回 `{code, reason, trace_id}` JSON |

### 不做（明确排除）
- 前端 SPA / HealthPage / ErrorPage
- `make migrate`、migrations/、seed 数据（属 M2）
- docker-compose（属 M7）
- 契约冻结 / `fingerprint.py`（属 M1）
- 插件本体、storage、scheduler 实现（`_app.py` lifespan 留空壳 + TODO 注释）

## 文件清单

### 新增
```
docs/plans/M0-implementation-plan.md   # 本实现计划落库（用户要求）
pyproject.toml                  # 依赖 + [project.entry-points.*] 三组占位
requirements.txt                # 运行依赖锁定
requirements-dev.txt            # dev 依赖（ruff/mypy/pytest/pre-commit/httpx）
.env.example                    # APM_* 环境变量样例
Makefile                        # install / lint / test / dev
.pre-commit-config.yaml         # ruff + mypy + 基础 hooks
src/aiops_apm/__init__.py
src/aiops_apm/settings.py       # Settings(BaseSettings), env_prefix="APM_"
src/aiops_apm/exceptions.py     # ErrorCode(7值) + AppException(code/reason/trace_id)
src/aiops_apm/_app.py           # create_app(settings) + lifespan + 异常处理中间件
src/aiops_apm/router/__init__.py
src/aiops_apm/router/api.py     # api_router: GET /health, GET /ready
tests/__init__.py
tests/conftest.py               # TestClient fixture（create_app(Settings())）
tests/test_health.py            # UC-0.1
tests/test_settings.py          # UC-0.2
tests/test_exceptions.py        # UC-0.3
docs/logs/M0.md                 # 实现日志（CLAUDE.md 流程要求）
docs/archive/M0-skeleton.md     # 归档已实现章节
```

### 修改
- `CLAUDE.md` — 实现流程规则补充：每个 M 阶段先出实现计划并存入 `docs/plans/<M>-implementation-plan.md`，CLAUDE.md 顶部标注「当前里程碑：M0（进行中）」；M0 完成后更新为「已完成」
- `README.md` — 同步实现进度（当前实现模块、用法、目录结构、完成状态）
- `docs/apm-alert-implementation-plan-enhanced.md` — M0 小节标记「已实现」，指向归档
- `.gitignore` — 确认已忽略 `.venv/`、`__pycache__/`、`.mypy_cache/`、`.ruff_cache/`、`.pytest_cache/`

## 关键实现细节

### `settings.py`（pydantic-settings v2）
```python
class Settings(BaseSettings):
    # 服务
    host: str = "0.0.0.0"
    port: int = 8000
    # 数据库
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "aiops_apm_runtime"
    # 调度
    scheduler_tick_sec: float = 1.0
    max_concurrent_rounds: int = 10
    total_timeout_sec: float = 30.0
    # 出站
    outbound_timeout_sec: float = 10.0
    outbound_max_body_bytes: int = 5_000_000
    # 开关
    enable_llm_summary: bool = False
    enable_scheduler: bool = True
    storage_backend: str = "mysql"   # mysql / memory
    model_config = SettingsConfigDict(env_prefix="APM_")
```
（文档给的是 pydantic v1 的 `class Config`，用 v2 的 `model_config` 写法。）

### `exceptions.py`
- `ErrorCode(str, Enum)`：`INTERNAL("INTERNAL_ERROR")` / `NOT_FOUND` / `VALIDATION("VALIDATION_ERROR")` / `PERMISSION("PERMISSION_DENIED")` / `PLUGIN_NOT_FOUND` / `CONFIG_ERROR` / `UPSTREAM_TIMEOUT`
- `AppException(Exception)`：`code: ErrorCode`、`reason: str`、`trace_id: str | None`，`__init__` 里 `super().__init__(reason)`

### `_app.py`
- `lifespan(app)` 异步上下文管理器：startup/shutdown 留空壳 + TODO（后续阶段填：连接池、插件加载、scheduler 启停）
- `create_app(settings: Settings | None = None)`：`FastAPI(title="APM Alert Module", lifespan=lifespan)`，`app.state.settings = settings`，`app.include_router(api_router)`（`settings=None` 时内部 `Settings()`，兼容 `uvicorn --factory`）
- 注册 3 个异常处理器，保证统一 JSON（UC-0.3）：
  - `AppException` → 按 code 映射 HTTP 状态（NOT_FOUND→404 / VALIDATION→400 / PERMISSION→403 / 其余→500），body `{code, reason, trace_id}`
  - `fastapi.HTTPException` → `{code, reason, trace_id}`（如 404 → `NOT_FOUND`）
  - `Exception`（兜底）→ 500 `{code:"INTERNAL_ERROR", reason, trace_id}`

### `router/api.py`
```python
api_router = APIRouter()

@api_router.get("/health")
async def health():                      # → {"status": "ok"}

@api_router.get("/ready")
async def ready():                       # M0: db=False, plugins=False → 503 NOT_READY
    checks = {"db": False, "plugins": False}
    # 后续 M 填真实检查；M0 空壳恒 503，满足 UC-0.1
```

### `pyproject.toml`
- `[build-system]` setuptools；`[project]` name=`aiops-apm-anomaly-detector`，`requires-python=">=3.10"`
- 运行依赖：`fastapi`、`uvicorn[standard]`、`pydantic>=2`、`pydantic-settings>=2`、`aiomysql`、`prometheus_client`
- dev 依赖：`pytest`、`pytest-asyncio`、`ruff`、`mypy`、`httpx`（TestClient）、`pre-commit`
- `[project.entry-points."aiops_apm.collectors"|"aiops_apm.detectors"|"aiops_apm.suppressors"]`：空表 + 注释占位（M4 填真插件）
- `[tool.setuptools.packages.find] where=["src"]`
- `[tool.ruff]` line-length=120 / target-version=py310；`[tool.mypy]` python_version=3.10；`[tool.pytest.ini_options]` pythonpath=["src"]、asyncio_mode="auto"

### Makefile
```make
install:  python -m pip install -e ".[dev]"
lint:     ruff check . && mypy src
test:     pytest -q
dev:      uvicorn aiops_apm._app:create_app --factory --reload --host 0.0.0.0 --port 8000
```

## 测试（TDD，先写测试再实现）

- `tests/test_health.py`（UC-0.1）：`TestClient` GET `/health` → 200 `{"status":"ok"}`；GET `/ready` → 503，body `code=="NOT_READY"`
- `tests/test_settings.py`（UC-0.2）：默认值断言；`monkeypatch.setenv("APM_PORT","9090")` → 新 `Settings()` 实例 `port==9090`
- `tests/test_exceptions.py`（UC-0.3）：GET 不存在路由 → 404 `{code:"NOT_FOUND", reason, trace_id}`；`AppException` 字段/枚举断言

## 验证（完成标准）

1. `make lint` — ruff + mypy 通过（含 `__init__.py`、test 文件，全绿）
2. `make test` — 3 个测试文件全过
3. `make dev` — uvicorn 启动，`curl localhost:8000/health` → 200；`curl localhost:8000/ready` → 503；`curl localhost:8000/nope` → 404 统一 JSON
4. `pip install -e ".[dev]"` 在干净 venv 可装

## 文档同步（CLAUDE.md 流程规则 + 本次新增要求）

1. 存实现计划：把本计划落库到 `docs/plans/M0-implementation-plan.md`（实现依据，可回溯）
2. 更新 `CLAUDE.md` 实现流程：规则补充「每个 M 阶段先出实现计划存入 `docs/plans/<M>-implementation-plan.md`」，并在顶部标注当前里程碑状态（M0 进行中/已完成）
3. 写 `docs/logs/M0.md`：改动点、新增文件清单、完成状态、遗留问题
4. 归档：建 `docs/archive/M0-skeleton.md`（M0 骨架规格），实现计划中 M0 小节标「已实现」并指向归档
5. 更新 `README.md`：实现进度、目录结构、`make` 用法、探针说明

## 实施步骤顺序

1. 建工程文件：`pyproject.toml`、`requirements*.txt`、`.env.example`、`Makefile`、`.pre-commit-config.yaml`、`.gitignore` 确认
2. 建 `src/aiops_apm/` 骨架：`settings.py` → `exceptions.py` → `_app.py` → `router/api.py`
3. 先写测试（`tests/`），再跑实现让测试通过（RED→GREEN）
4. 本地 venv 安装 + `make lint` + `make test` 全绿
5. `make dev` 手动验证探针（health/ready/404）
6. 文档同步：存计划 `docs/plans/M0-implementation-plan.md`、更新 `CLAUDE.md`、`docs/logs/M0.md`、`docs/archive/M0-skeleton.md`、实现计划标记、README 更新
