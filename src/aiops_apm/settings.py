"""应用配置（pydantic-settings）。

所有配置项以环境变量覆盖，前缀为 ``APM_``（如 ``APM_PORT``、``APM_DB_HOST``）。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置，后续里程碑按需扩展（调度、出站、降级开关等）。"""

    # ---- 服务 ----
    host: str = "0.0.0.0"
    port: int = 8000

    # ---- 数据库（M2 起生效，M0 仅为占位）----
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "aiops_apm_runtime"

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

    # ---- 鉴权（M6 起生效，配置了才强制）----
    # JSON env APM_API_KEYS：{"<api-key>": "tenant1,tenant2"}，值 "*" 表全租户。
    # 为空 = 放行（不挂 AuthMiddleware，既有 API 测试零改动）。
    api_keys: dict[str, str] = {}

    # ---- 可观测性 / 安全（M7 起生效）----
    # 安全审计日志开关（APM_AUDIT_ENABLED，默认开；日志即审计，不落库）。
    audit_enabled: bool = True
    # 轮次审计 list_rounds 默认取轮次数上限（APM_ROUND_RETENTION_ROUNDS）。
    round_retention_rounds: int = 1000

    # ---- 出站（M3 起生效）----
    outbound_timeout_sec: float = 10.0
    outbound_max_body_bytes: int = 5_000_000

    # ---- 开关 ----
    enable_llm_summary: bool = False
    enable_scheduler: bool = True
    # mysql（生产）/ memory（本地 demo/单测，不引入 SQLite）
    storage_backend: str = "mysql"

    model_config = SettingsConfigDict(env_prefix="APM_", extra="ignore", env_file=".env")
