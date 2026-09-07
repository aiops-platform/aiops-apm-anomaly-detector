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


def test_signature_keeps_full_message_and_line_numbers():
    sig = signature(_log())
    assert sig == "OutOfMemoryError: heap space|at com.A.run(A.java:10)|at com.B.run(B.java:20)|at com.C.run(C.java:30)"
    assert "java:10" in sig  # 保留行号（用户确认：消息 + 行号都要）
    assert "heap space" in sig  # 保留异常消息


def test_signature_n_frames_truncates():
    assert signature(_log(), n_frames=2) == "OutOfMemoryError: heap space|at com.A.run(A.java:10)|at com.B.run(B.java:20)"


def test_signature_fallback_to_message_without_stack_trace():
    log = _log(stack_trace=None)
    assert signature(log) == log.message[:120]
