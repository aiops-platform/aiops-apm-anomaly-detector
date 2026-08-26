"""日志堆栈签名纯函数（L1 ``signature_aggregate`` 检测 / M3 ``http_logs`` 预计算共享）。

设计文档 §6.4：
``signature = 异常类型 + 顶部 N 帧（类名+方法名，去行号，N=3~5）``；
无堆栈时回退 ``log.message[:120]``。确定性纯函数，不依赖外部状态。
"""

from __future__ import annotations

from .models.signal import LogSignal


def signature(log: LogSignal, n_frames: int = 3) -> str:
    """计算日志堆栈签名：``异常类型|顶部N帧``；无堆栈时取 message 前 120 字符。"""
    if not log.stack_trace:
        return log.message[:120]
    lines = log.stack_trace.strip().split("\n")
    exc = lines[0].split(":")[0] if lines else log.message
    frames = [ln.strip().split("(")[0] for ln in lines[1 : 1 + n_frames]]
    return "|".join([exc, *frames])
