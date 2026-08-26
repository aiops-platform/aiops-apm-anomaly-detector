"""UC-3.4 日志堆栈签名：``signature()`` 纯函数。"""

from datetime import datetime

from aiops_apm.models.signal import LogSignal
from aiops_apm.signature import signature

STACK = (
    "OutOfMemoryError: heap space\n"
    "\tat com.A.run(A.java:10)\n"
    "\tat com.B.run(B.java:20)\n"
    "\tat com.C.run(C.java:30)\n"
    "\tat com.D.run(D.java:40)"
)


def _log(**overrides):
    base = dict(
        service="order",
        level="ERROR",
        message="OutOfMemoryError",
        stack_trace=STACK,
        timestamp=datetime(2026, 8, 26, 12, 0, 0),
    )
    base.update(overrides)
    return LogSignal(**base)


def test_signature_uses_exception_type_and_top_frames_without_line_numbers():
    sig = signature(_log())
    assert sig == "OutOfMemoryError|at com.A.run|at com.B.run|at com.C.run"
    assert "java:10" not in sig  # 去行号


def test_signature_n_frames_truncates():
    assert signature(_log(), n_frames=2) == "OutOfMemoryError|at com.A.run|at com.B.run"


def test_signature_fallback_to_message_without_stack_trace():
    log = _log(stack_trace=None)
    assert signature(log) == log.message[:120]
