"""日志堆栈签名纯函数（L1 ``signature_aggregate`` 检测 / M3 ``http_logs`` 预计算共享）。

设计文档 §6.4（2026-08-28 用户确认调整）：
``signature = 完整异常首行（异常类型 + 消息）+ 顶部 N 帧（类名.方法(文件:行号)，N=3~5）``；
无堆栈时回退 ``log.message[:120]``。确定性纯函数，不依赖外部状态。
"""

from __future__ import annotations

from .models.signal import LogSignal

# 堆栈签名上限：signal_snapshot.signature 为 VARCHAR(1024)（V7 加宽，V1 原为 255）。
# 保留完整消息 + 行号后签名显著变长（Spring 异常 ≈ 500+），深包名 + 多帧仍可能超限，
# 写库报 DataError 1406 拖垮整轮采集。上限取 1000 留余量；截断在尾部，确定性一致。
MAX_SIGNATURE_LEN = 1000


def signature(log: LogSignal, n_frames: int = 3) -> str:
    """计算日志堆栈签名：``完整异常首行|顶部N帧(含行号)``；无堆栈时取 message 前 120 字符。"""
    if not log.stack_trace:
        sig = log.message[:120]
    else:
        lines = log.stack_trace.strip().split("\n")
        exc = lines[0] if lines else log.message  # 完整首行：异常类型 + 消息（保留冒号后内容）
        frames = [ln.strip() for ln in lines[1 : 1 + n_frames]]  # 帧保留类名.方法(文件:行号)
        sig = "|".join([exc, *frames])
    return sig[:MAX_SIGNATURE_LEN]
