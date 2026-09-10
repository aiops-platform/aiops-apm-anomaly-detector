"""应用级异常与错误码。

所有对外返回的错误统一为 ``{code, reason, trace_id}`` 结构（见 UC-0.3）。
"""

from enum import Enum


class ErrorCode(str, Enum):
    """业务错误码，与对外 JSON 的 ``code`` 字段一致。"""

    INTERNAL = "INTERNAL_ERROR"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION = "VALIDATION_ERROR"
    PERMISSION = "PERMISSION_DENIED"
    PLUGIN_NOT_FOUND = "PLUGIN_NOT_FOUND"
    CONFIG_ERROR = "CONFIG_ERROR"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    # 状态冲突（如对非 pending 的问题发起 Analyze）→ HTTP 409。
    CONFLICT = "STATE_CONFLICT"
    # 上游 agentflow 调用失败（连接/超时/非 2xx）→ HTTP 502。
    UPSTREAM = "UPSTREAM_ERROR"


class AppException(Exception):
    """应用可预期异常，携带错误码与人类可读原因。"""

    def __init__(self, code: ErrorCode, reason: str, trace_id: str | None = None) -> None:
        self.code = code
        self.reason = reason
        self.trace_id = trace_id
        super().__init__(reason)
