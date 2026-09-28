"""UC-3.4 日志堆栈签名：``signature()`` 纯函数 + ``normalize_message`` 变量归一化。"""

from datetime import datetime

from aiops_apm.models.signal import LogSignal
from aiops_apm.signature import normalize_message, signature

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


# ── 变量归一化（2026-09-28）────────────────────────────────────────────────
# 回归背景：同一故障在 3 轮里开了 3 张单（PR-20260928-0352/0354/0356）。正文里带
# per-request id ⇒ 每个请求一个新签名 ⇒ 新 anomaly_key ⇒ group_key（组内异常集合的
# 哈希）每轮都变 ⇒ 去重闸门恒不命中。下面两条守的就是「同一模板不同取值 → 同一签名」。


def test_signature_normalizes_per_request_ids_in_message_without_stack_trace():
    """无堆栈 + 正文带 orderId/traceId：三个请求必须收敛到同一个签名。"""
    sigs = {
        signature(_log(stack_trace=None, message=m))
        for m in (
            "报价单模板缺失 orderId=ORD-1 traceId=91ca5f3f-650c-40c5-9d44-806a55aa44ff",
            "报价单模板缺失 orderId=ORD001 traceId=fdf9d068-cb3b-4c8d-88c6-7a208184b71b",
            "报价单模板缺失 orderId=ORD-1 traceId=f5ec98ac-05a1-4459-a302-5bce3804fd4c",
        )
    }
    assert sigs == {"报价单模板缺失 orderId=<*> traceId=<*>"}


def test_signature_normalizes_exception_message_but_not_frames():
    """堆栈分支：异常首行的值归一化，**帧逐字保留**（帧是签名的锚）。"""
    log = _log(stack_trace="QuotationException: 模板缺失 orderId=ORD-9\n\tat com.A.run(A.java:10)")
    assert signature(log) == "QuotationException: 模板缺失 orderId=<*>|at com.A.run(A.java:10)"


def test_signature_keeps_different_templates_apart():
    """归一化只动值：模板文本不同 → 签名必须仍然不同（M9「不同签名各自成单」）。"""
    a = signature(_log(stack_trace=None, message="报价单模板缺失 orderId=A"))
    b = signature(_log(stack_trace=None, message="订单号格式非法 orderId=A"))
    assert a != b


def test_normalize_message_leaves_non_kv_text_alone():
    """不该动的别动：比较运算、无值、无 = 的正文。"""
    assert normalize_message("a==b") == "a==b"           # 值里排除 =（不加这条会变 a=<*>）
    assert normalize_message("a == b") == "a == b"       # 键后必须紧跟 =（空格隔开不算）
    assert normalize_message("count>=5") == "count>=5"
    assert normalize_message("foo=") == "foo="           # 空值不匹配
    assert normalize_message("订单号格式非法") == "订单号格式非法"
    assert normalize_message("Failed to resolve 'order-service'") == "Failed to resolve 'order-service'"


def test_normalize_message_handles_separators_and_dotted_keys():
    """值到空白/分隔符为止；点分键名整体作键。"""
    assert normalize_message("a=b,c=d") == "a=<*>,c=<*>"
    assert normalize_message("app.name=order-service level=ERROR") == "app.name=<*> level=<*>"
    # 半个标识符不能被当成键（前置断言）：整个 xabc 是键
    assert normalize_message("xabc=1") == "xabc=<*>"
