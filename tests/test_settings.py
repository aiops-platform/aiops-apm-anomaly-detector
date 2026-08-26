"""UC-0.2 环境变量覆盖配置。"""

from aiops_apm.settings import Settings


def test_defaults() -> None:
    s = Settings()
    assert s.port == 8000
    assert s.db_name == "aiops_apm_runtime"
    assert s.enable_llm_summary is False
    assert s.storage_backend == "mysql"


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("APM_PORT", "9090")
    assert Settings().port == 9090
