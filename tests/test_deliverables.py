"""UC-7.4/7.5 交付物静态断言：Docker / compose / mock 源 / seed / 自定义插件 / demo / 压测。

本机无 docker/locust → 不做真容器验证，仅断言文件存在与关键内容（照既有「写出待补跑」模式）。
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent
DOCKER = ROOT / "docker"


def test_dockerfile_exists_and_runs_uvicorn() -> None:
    df = DOCKER / "Dockerfile"
    assert df.exists()
    text = df.read_text()
    assert "uvicorn" in text
    assert "aiops_apm.main:app" in text
    # 多阶段构建（builder → runtime）
    assert "AS builder" in text
    assert "AS runtime" in text


def test_dockerfile_mock_source_exists() -> None:
    assert (DOCKER / "Dockerfile.mock-source").exists()


def test_docker_compose_has_core_services() -> None:
    text = (DOCKER / "docker-compose.yml").read_text()
    for svc in ("mysql", "mock-source", "apm-alert", "prometheus"):
        assert f"  {svc}:" in text
    assert "APM_STORAGE_BACKEND: mysql" in text
    assert "aiops_apm.migrations.runner" in text
    assert "seed.py" in text
    assert "condition: service_healthy" in text  # mysql 健康检查门控


def test_prometheus_config_scrapes_apm_alert() -> None:
    text = (DOCKER / "prometheus.yml").read_text()
    assert "apm-alert:8000" in text
    assert "metrics_path: /metrics" in text


def test_mock_source_serves_metrics_and_logs() -> None:
    src = (DOCKER / "mock_source.py").read_text()
    assert '"/metrics"' in src
    assert '"/logs"' in src
    assert "http.server" in src or "HTTPServer" in src  # stdlib only
    assert "9100" in src


def test_seed_writes_targets_and_domain_config() -> None:
    text = (DOCKER / "seed.py").read_text()
    assert "monitor_targets.create" in text
    assert "domain_configs.upsert" in text
    assert '"source_type": "http_metrics"' in text
    assert '"source_type": "http_logs"' in text


def test_custom_detector_has_entry_point_and_build() -> None:
    pkg_dir = DOCKER / "custom_detector"
    pyproject = (pkg_dir / "pyproject.toml").read_text()
    assert "aiops_apm.detectors" in pyproject
    assert "p95_latency = \"p95_latency:build\"" in pyproject
    init = (pkg_dir / "p95_latency" / "__init__.py").read_text()
    assert "class P95LatencyDetector(Detector)" in init
    assert "def build(" in init


def test_demo_script_checks_problems_audit_metrics() -> None:
    text = (DOCKER / "demo.py").read_text()
    assert "/v1/problems" in text
    assert "/v1/audit/rounds" in text
    assert "/v1/audit/suppressed" in text
    assert "/metrics" in text


def test_locustfile_hits_read_and_write_paths() -> None:
    text = (DOCKER / "locustfile.py").read_text()
    assert "HttpUser" in text
    assert "self.client.get" in text
    assert "/v1/alerts/run" in text
    assert "/v1/problems" in text


def test_makefile_has_docker_and_loadtest_targets() -> None:
    text = (ROOT / "Makefile").read_text()
    assert "docker-up:" in text
    assert "docker-down:" in text
    assert "loadtest:" in text
