"""UC-1.1 Signal 序列化与判别器。

三个 Signal 各做 ``model_dump_json()`` → ``TypeAdapter(Signal).validate_json()``，
反序列化后应还原为对应的具体类型且 ``kind`` 正确。
"""

from datetime import datetime

from pydantic import TypeAdapter

from aiops_apm.models.signal import ChangeSignal, LogSignal, MetricSignal, Signal

_NOW = datetime(2026, 8, 26, 12, 0, 0)


def test_metric_signal_roundtrip() -> None:
    s = MetricSignal(service="checkout", metric="cpu_usage", value=0.91, timestamp=_NOW)
    restored = TypeAdapter(Signal).validate_json(s.model_dump_json())
    assert isinstance(restored, MetricSignal)
    assert restored.kind == "metric"
    assert restored.service == "checkout"
    assert restored.metric == "cpu_usage"
    assert restored.value == 0.91
    assert restored.tenant_id == "default"


def test_log_signal_roundtrip() -> None:
    s = LogSignal(service="checkout", level="ERROR", message="boom", timestamp=_NOW, trace_id="t1")
    restored = TypeAdapter(Signal).validate_json(s.model_dump_json())
    assert isinstance(restored, LogSignal)
    assert restored.kind == "log"
    assert restored.level == "ERROR"
    assert restored.message == "boom"
    assert restored.trace_id == "t1"


def test_change_signal_roundtrip() -> None:
    s = ChangeSignal(service="checkout", change_id="c1", type="deployment", summary="deploy v2", timestamp=_NOW)
    restored = TypeAdapter(Signal).validate_json(s.model_dump_json())
    assert isinstance(restored, ChangeSignal)
    assert restored.kind == "change"
    assert restored.change_id == "c1"
    assert restored.type == "deployment"
