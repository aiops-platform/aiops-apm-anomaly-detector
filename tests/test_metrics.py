"""UC-7.1 Prometheus 指标：定义存在 / 打点增量 / /metrics 端点暴露。

prometheus_client 用全局注册表，测试用**相对增量**断言（先取 before → 打点 → after），
避免跨用例的全局计数污染。
"""

import pytest

from aiops_apm._app import create_app
from aiops_apm.metrics import (
    DEGRADED_SOURCES,
    FALSE_POSITIVE_RATE,
    RECORDS_CREATED,
    ROUND_DURATION,
    ROUND_SUCCESS,
    ROUND_TOTAL,
    SUPPRESSED_TOTAL,
    record_round_metrics,
    update_fpr_gauge,
)
from aiops_apm.pipeline.context import DomainResult
from aiops_apm.settings import Settings


def _result(*, timeline=None, degraded=None) -> DomainResult:
    return DomainResult(
        domain="application",
        records=[],  # 由用例填充
        suppressed_count=0,
        anomaly_count=0,
        degraded_sources=degraded or [],
        timeline=timeline or [{"step": "collect_done", "count": 0}],
    )


def test_metric_definitions_exist() -> None:
    # 7 类指标定义在模块级（UC-7.1 断言）
    assert ROUND_TOTAL._type == "counter"
    assert ROUND_SUCCESS._type == "counter"
    assert RECORDS_CREATED._type == "counter"
    assert DEGRADED_SOURCES._type == "counter"
    assert SUPPRESSED_TOTAL._type == "counter"
    assert FALSE_POSITIVE_RATE._type == "gauge"
    assert ROUND_DURATION._type == "histogram"


def test_record_round_metrics_increments_counts() -> None:
    before = ROUND_TOTAL.labels("application", "default", "success")._value.get()
    result = _result()
    record_round_metrics(domain="application", tenant_id="default", status="success", duration_sec=0.1, result=result)
    after = ROUND_TOTAL.labels("application", "default", "success")._value.get()
    assert after > before  # 相对增量断言，避免全局注册表污染
    assert ROUND_DURATION.labels("application", "default")._sum.get() > 0


def test_record_round_metrics_counts_degraded_and_suppressed() -> None:
    degraded_before = DEGRADED_SOURCES.labels("default")._value.get()
    sup_before = SUPPRESSED_TOTAL.labels("svc-a", "maintenance_window")._value.get()
    timeline = [
        {"step": "collect_done", "count": 2},
        {
            "step": "suppressed",
            "count": 1,
            "details": [
                {"signal": "metric:heap_usage", "service": "svc-a", "suppressor": "maintenance_window", "reason": "mw"}
            ],
        },
    ]
    result = _result(timeline=timeline, degraded=["MT-0009"])
    record_round_metrics(
        domain="application", tenant_id="default", status="partial", duration_sec=0.2, result=result
    )
    assert DEGRADED_SOURCES.labels("default")._value.get() > degraded_before
    assert SUPPRESSED_TOTAL.labels("svc-a", "maintenance_window")._value.get() > sup_before


def test_update_fpr_gauge_sets_mean_by_service() -> None:
    fpr_data = {
        "default:application:svc-a:aaa": {"fpr": 0.5, "total": 2},
        "default:application:svc-a:bbb": {"fpr": 0.7, "total": 2},
        "default:application:svc-b:ccc": {"fpr": 1.0, "total": 1},
        "other:application:svc-a:ddd": {"fpr": 0.0, "total": 0},  # 不同租户 / total=0 均排除
    }
    update_fpr_gauge("default", "application", "svc-a", fpr_data)
    assert FALSE_POSITIVE_RATE.labels("svc-a")._value.get() == pytest.approx(0.6)


def test_metrics_endpoint_exposes_counters() -> None:
    app = create_app(Settings(_env_file=None, storage_backend="memory", enable_scheduler=False))
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        resp = c.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        assert "aiops_round_total" in body
        assert "aiops_false_positive_rate" in body
        assert "text/plain" in resp.headers["content-type"]
