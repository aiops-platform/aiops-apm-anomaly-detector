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

    # ---- 出站（M3 起生效）----
    outbound_timeout_sec: float = 10.0
    outbound_max_body_bytes: int = 5_000_000

    # ---- 开关 ----
    enable_llm_summary: bool = False
    enable_scheduler: bool = True
    # mysql（生产）/ memory（本地 demo/单测，不引入 SQLite）
    storage_backend: str = "mysql"

    model_config = SettingsConfigDict(env_prefix="APM_", extra="ignore", env_file=".env")
