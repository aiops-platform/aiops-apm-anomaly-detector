"""UC-1.2 Anomaly 指纹稳定性 + UC-1.3 Group Key 排序无关性。

指纹是去重/L3 持续性的真源：相同问题身份恒等，与 value/severity/时间无关。
"""

from datetime import datetime

from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly
from aiops_apm.models.fingerprint import anomaly_key, group_key, is_same_group
from aiops_apm.models.record import Correlation, ProblemRecord, Verification

_NOW = datetime(2026, 8, 26, 12, 0, 0)


def _metric(**overrides: object) -> MetricAnomaly:
    base: dict[str, object] = {
        "tenant_id": "default",
        "service": "checkout",
        "metric": "cpu_usage",
        "value": 0.95,
        "method": "static_threshold",
        "severity": "warning",
        "detected_at": _NOW,
        "labels": {"host": "a"},
    }
    base.update(overrides)
    return MetricAnomaly(**base)  # type: ignore[arg-type]


# ---- UC-1.2 anomaly_key 稳定性 ----
def test_anomaly_key_stable_for_same_input() -> None:
    assert anomaly_key(_metric()) == anomaly_key(_metric())


def test_anomaly_key_differs_on_labels() -> None:
    assert anomaly_key(_metric(labels={"host": "a"})) != anomaly_key(_metric(labels={"host": "b"}))


def test_anomaly_key_differs_on_service() -> None:
    assert anomaly_key(_metric(service="checkout")) != anomaly_key(_metric(service="payment"))


def test_anomaly_key_ignores_value_and_severity() -> None:
    # 指纹表达「问题身份」而非「当前读数」：值/严重度变化不改变 key
    assert anomaly_key(_metric(value=0.5, severity="warning")) == anomaly_key(
        _metric(value=0.99, severity="critical")
    )


def test_anomaly_key_method_matches_function() -> None:
    a = _metric()
    assert a.anomaly_key() == anomaly_key(a)


def test_log_anomaly_key_by_signature() -> None:
    def log(signature: str) -> LogAnomaly:
        return LogAnomaly(
            tenant_id="default",
            service="checkout",
            level="ERROR",
            signature=signature,
            pattern=signature,
            count=5,
            first_seen=_NOW,
            severity="warning",
        )

    assert anomaly_key(log("sig-a")) == anomaly_key(log("sig-a"))
    assert anomaly_key(log("sig-a")) != anomaly_key(log("sig-b"))


# ---- UC-1.3 group_key 排序无关性 ----
def test_group_key_order_independent() -> None:
    a1 = _metric(metric="cpu_usage")
    a2 = _metric(metric="mem_usage")
    a3 = _metric(metric="disk_usage")
    g1 = group_key("default", "infra", "checkout", [a1, a2, a3])
    g2 = group_key("default", "infra", "checkout", [a3, a1, a2])
    assert g1 == g2


def test_group_key_format() -> None:
    g = group_key("default", "infra", "checkout", [_metric()])
    prefix = "default:infra:checkout:"
    assert g.startswith(prefix)
    assert len(g) == len(prefix) + 12  # sha256[:12]


def test_is_same_group() -> None:
    g1 = group_key("default", "infra", "checkout", [_metric()])
    g2 = group_key("default", "infra", "checkout", [_metric()])
    assert is_same_group(g1, g2)
    assert not is_same_group(g1, "other:domain:svc:x")


def test_problem_record_group_key_property() -> None:
    ma = _metric()
    rec = ProblemRecord(
        record_id="r1",
        domain="infra",
        service="checkout",
        detected_at=_NOW,
        symptom={"summary": "cpu high"},
        metric_anomalies=[ma],
        log_anomalies=[],
        correlation=Correlation(related=False, reason="none"),
        verification=Verification(passed=True, persistence_ok=True, final_severity="warning"),
    )
    assert rec.group_key == group_key("default", "infra", "checkout", [ma])
