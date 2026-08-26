"""M6 §13 用例 2：``calibrate_severity`` 组合升 critical（related + high metric + high log）。"""

from datetime import datetime, timezone

from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly
from aiops_apm.pipeline.l3_verify import calibrate_severity

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def ma(*, severity="high", metric="heap_usage") -> MetricAnomaly:
    return MetricAnomaly(
        service="svc-a", metric=metric, value=0.98, method="static_threshold", severity=severity, detected_at=TS
    )


def la(*, severity="high") -> LogAnomaly:
    return LogAnomaly(
        service="svc-a", level="ERROR", signature="java.lang.OOMError", pattern="oom", count=47,
        first_seen=TS, severity=severity, detected_at=TS,
    )


# --- calibrate_severity(related=True) 组合升 critical ---


def test_combo_high_metric_high_log_related_critical() -> None:
    assert calibrate_severity([ma(severity="high"), la(severity="high")], related=True) == "critical"


def test_combo_not_related_keeps_max() -> None:
    assert calibrate_severity([ma(severity="high"), la(severity="high")], related=False) == "high"


def test_combo_missing_high_side_keeps_max() -> None:
    # 只有 metric high，log warning → 无组合
    assert calibrate_severity([ma(severity="high"), la(severity="warning")], related=True) == "high"
    # 只有 log high，metric warning → 无组合
    assert calibrate_severity([ma(severity="warning"), la(severity="high")], related=True) == "high"


def test_combo_all_warning_stays_warning() -> None:
    assert calibrate_severity([ma(severity="warning"), la(severity="warning")], related=True) == "warning"


def test_combo_top_already_critical_unchanged() -> None:
    assert calibrate_severity([ma(severity="critical"), la(severity="high")], related=True) == "critical"


def test_combo_single_kind_does_not_combo() -> None:
    # 只有 metric（无 log）→ related 也无法组合
    assert calibrate_severity([ma(severity="high")], related=True) == "high"
