"""日志堆栈签名纯函数（L1 ``signature_aggregate`` 检测 / M3 ``http_logs`` 预计算共享）。

设计文档 §6.4（2026-08-28 用户确认调整）：
``signature = 完整异常首行（异常类型 + 消息）+ 顶部 N 帧（类名.方法(文件:行号)，N=3~5）``；
无堆栈时回退 ``log.message[:120]``。确定性纯函数，不依赖外部状态。

⚠️ 2026-09-28 追加**变量归一化**（``normalize_message``）：消息正文里的 ``key=value``
一律替成 ``key=<*>``。原因是签名不只给人看，它还是**去重身份**
（``fingerprint.anomaly_key = log|tenant|service|signature``，再经 ``group_key`` 决定
``problem_record`` 是否追加）。正文里带 per-request id 时（``… orderId=ORD-1 traceId=91ca5f3f-…``）
每个请求都产出一个**新签名 ⇒ 新 anomaly_key ⇒ 新 group_key ⇒ 新开一张单**，
而 group_key 是「组内异常集合」的哈希，成员一变就换键，去重闸门恒不命中。
实测：同一故障在 3 轮里开了 3 张单（PR-20260928-0352/0354/0356）。
"""

from __future__ import annotations

import re

from .models.signal import LogSignal

# 堆栈签名上限：signal_snapshot.signature 为 VARCHAR(1024)（V7 加宽，V1 原为 255）。
# 保留完整消息 + 行号后签名显著变长（Spring 异常 ≈ 500+），深包名 + 多帧仍可能超限，
# 写库报 DataError 1406 拖垮整轮采集。上限取 1000 留余量；截断在尾部，确定性一致。
MAX_SIGNATURE_LEN = 1000

# 无堆栈时的消息窗口。**先截断再归一化**（不是反过来）：窗口与改动前逐字相同，
# 于是这次改动的语义收敛成「同一个窗口内的 ``=`` 值被替换」，不会因为归一化后
# 变短而把原本被切掉的后文放进来——那会让签名在另一个方向上发生漂移。
MESSAGE_LIMIT = 120

# ``key=value``：键是标识符（允许点分，如 ``app.name=``），值跟到空白或常见分隔符为止。
# 值里排除 ``=``，否则 ``a==b`` 这类比较会被当成 ``a=<*>`` 吃掉（``a==b`` 应当**不动**）。
# 前置断言挡住 ``xabc=1`` 里的 ``abc=1``：那样会替掉半个标识符。
_KV = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z_][A-Za-z0-9_.]*)=([^\s=,;)\]}\"']+)")


def normalize_message(text: str) -> str:
    """把消息正文里的 ``key=value`` 变量替成 ``key=<*>``（签名稳定性的来源）。

    只动「值」：键名保留，模板文本保留，从而同类问题跨请求恒等，而不同类问题
    （异常类型、堆栈帧、模板文本不同）依旧分开。无 ``=`` 的正文原样返回。
    """
    return _KV.sub(r"\1=<*>", text)


def signature(log: LogSignal, n_frames: int = 3) -> str:
    """计算日志堆栈签名：``完整异常首行|顶部N帧(含行号)``；无堆栈时取 message 前 120 字符。

    两条分支的消息正文都过 ``normalize_message``；**堆栈帧不做归一化**——帧是签名的
    锚（类名.方法(文件:行号)），且含 ``=`` 的帧极罕见，不动它更安全。
    """
    if not log.stack_trace:
        sig = normalize_message(log.message[:MESSAGE_LIMIT])
    else:
        lines = log.stack_trace.strip().split("\n")
        exc = lines[0] if lines else log.message  # 完整首行：异常类型 + 消息（保留冒号后内容）
        frames = [ln.strip() for ln in lines[1 : 1 + n_frames]]  # 帧保留类名.方法(文件:行号)
        sig = "|".join([normalize_message(exc), *frames])
    return sig[:MAX_SIGNATURE_LEN]
