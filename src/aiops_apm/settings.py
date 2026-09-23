"""应用配置（pydantic-settings）。

所有配置项以环境变量覆盖，前缀为 ``APM_``（如 ``APM_PORT``、``APM_DB_HOST``）。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置，后续里程碑按需扩展（调度、出站、降级开关等）。"""

    # ---- 服务 ----
    host: str = "0.0.0.0"
    port: int = 8000

    # ---- 数据库（PostgreSQL）----
    # 默认值对齐 multi-agent-workflow 的 docker compose（postgres:17，db/user 均为 agentflow）。
    # 本模块的表建在 db_name 库内的 **独立 schema** db_schema 下，与 agentflow 自己在 public
    # 里的表隔离——这是 MySQL「单 schema aiops_apm_runtime」在 PG 下的对应物。
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_user: str = "agentflow"
    db_password: str = ""
    db_name: str = "agentflow"
    db_schema: str = "aiops_apm_runtime"

    # ---- 调度器 ----
    scheduler_tick_sec: float = 1.0
    max_concurrent_rounds: int = 10
    total_timeout_sec: float = 30.0

    # ---- 多副本 lease（M6 起生效）----
    scheduler_lease_ttl_sec: float = 30.0
    scheduler_jitter_ratio: float = 0.1

    # ---- 恢复闭环（M6 起生效，reconcile 自动关闭）----
    resolve_after_rounds: int = 3
    resolve_check_interval_sec: float = 30.0
    # 自动关单后台任务开关（APM_ENABLE_RECONCILER，默认开）。false = 只关自动关单，
    # 检测轮次照常跑；单靠后端手动 POST /v1/problems/{id}/resolve 关闭。
    enable_reconciler: bool = True

    # ---- 鉴权（M6 起生效，配置了才强制）----
    # JSON env APM_API_KEYS：{"<api-key>": "tenant1,tenant2"}，值 "*" 表全租户。
    # 为空 = 放行（不挂 AuthMiddleware，既有 API 测试零改动）。
    api_keys: dict[str, str] = {}

    # ---- 前端 CORS（本计划 §8.1，配置了才挂中间件；空=不挂）----
    # APM_ALLOWED_ORIGINS：逗号分隔或 JSON 数组，如 "http://localhost:8080" 或 '["http://localhost:8080"]'。
    allowed_origins: list[str] = []

    # ---- SSRF 出站网关（本地联调开关）----
    # APM_ALLOW_LOOPBACK：true 放行回环地址（localhost/127.0.0.1/::1），本地联调用；
    # 默认 false = 保持 fail-closed 全拦截。内网/云元数据（10/172.16/192.168/169.254）始终拦截。
    allow_loopback: bool = False

    # ---- 可观测性 / 安全（M7 起生效）----
    # 安全审计日志开关（APM_AUDIT_ENABLED，默认开；日志即审计，不落库）。
    audit_enabled: bool = True
    # 轮次审计 list_rounds 默认取轮次数上限（APM_ROUND_RETENTION_ROUNDS）。
    round_retention_rounds: int = 1000

    # ---- 出站（M3 起生效）----
    outbound_timeout_sec: float = 10.0
    outbound_max_body_bytes: int = 5_000_000
    # agentflow（multi-agent-workflow，Bug Solve 后端）根地址：Problem Center Analyze 联动 POST /run 用。
    # APM_BUG_SOLVE_BASE_URL 可覆盖。
    bug_solve_base_url: str = "http://localhost:8000"
    # 起 agentflow run 的**专用**超时（秒），不共用 outbound_timeout_sec。
    # agentflow 的 POST /run 在返回前会**同步准备工作区**（拉修复侧仓库），实测超过 10s ——
    # 用共用的 10s 会让它稳定超时。而且超时**不会阻止 agentflow 把 run 跑起来**：
    # 于是我们报失败、它照跑，用户看到失败就重试 → **每点一次多一个孤儿 run**，
    # 每个孤儿还占租户并发配额。故这里给足余量。
    run_start_timeout_sec: float = 60.0
    # agentflow 侧租户（出站 X-Tenant-ID）。**两侧租户不是同一个**：问题单在 APM 租户下
    # （本机 default，前端用 X-Tenant-Id 指定该值），而 workflow / MCP server / agent 绑定
    # 在 agentflow 租户下（本机 otr）。必须显式桥接——否则 run 落到 agentflow 的 dev 缺省
    # 租户 `local`，页面上表现为 "Failed to load run: HTTP 404"（跨租户 run 一律 404）。
    # 空 = 原样转发请求租户（单租户部署行为不变）。APM_AGENTFLOW_TENANT 可覆盖。
    agentflow_tenant: str = ""
    # 诊断服务（HolmesGPT spike：aiops-agent-orchestration-spike）根地址：Problem Center「分析new」
    # 按问题单拼装 POST /diagnose/logs 用（同源读 GET /status/{session_id}）。APM_DIAGNOSE_BASE_URL 可覆盖。
    diagnose_base_url: str = "http://localhost:8017"
    # 诊断请求的 repo 定位（spike 侧 repo 即仓库定位）。problem_record 无 repo 字段 →
    # 缺省用本项；仍为空则回退 record.service。APM_DIAGNOSE_REPO 可覆盖。
    diagnose_repo: str = ""

    # ---- 测试床（V9 迁移 seed 的日志监控端点用）----
    # 三个服务（order/warranty/gateway）的日志都经 filebeat 进 Elasticsearch，
    # 本机是 kubectl port-forward 出来的 19200。**容器里 localhost 指向容器自己**，
    # 所以从 compose 里跑要改成 host.containers.internal:19200。
    # 迁移是静态 SQL 读不到环境变量，由 MigrationRunner 以 GUC 注入（见 runner.py）。
    testbed_es_url: str = "http://localhost:19200/app-logs/_search"

    # ---- 开关 ----
    enable_llm_summary: bool = False
    enable_scheduler: bool = True
    # pg（生产，PostgreSQL）/ memory（本地 demo/单测，不引入 SQLite）
    storage_backend: str = "pg"

    model_config = SettingsConfigDict(env_prefix="APM_", extra="ignore", env_file=".env")
