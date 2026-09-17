"""UC-0.2 环境变量覆盖配置。"""

from aiops_apm.settings import Settings


def test_defaults() -> None:
    # _env_file=None：隔离本地 .env（如 APM_PORT=7070），断言纯默认值
    s = Settings(_env_file=None)
    assert s.port == 8000
    # 默认值对齐 multi-agent-workflow 的 PG（库 agentflow，表建在独立 schema 下）
    assert s.db_name == "agentflow"
    assert s.db_schema == "aiops_apm_runtime"
    assert s.db_port == 5432
    assert s.enable_llm_summary is False
    assert s.storage_backend == "pg"


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("APM_PORT", "9090")
    assert Settings().port == 9090


def test_bug_solve_base_url_override(monkeypatch) -> None:
    monkeypatch.setenv("APM_BUG_SOLVE_BASE_URL", "http://agentflow:8000")
    assert Settings().bug_solve_base_url == "http://agentflow:8000"


def test_enable_reconciler_defaults_on_and_overridable(monkeypatch) -> None:
    assert Settings(_env_file=None).enable_reconciler is True
    monkeypatch.setenv("APM_ENABLE_RECONCILER", "false")
    assert Settings(_env_file=None).enable_reconciler is False
