"""UC-0.3 异常标准化响应。"""

from fastapi.testclient import TestClient

from aiops_apm.exceptions import AppException, ErrorCode


def test_not_found_unified_json(client: TestClient) -> None:
    """请求不存在的资源应返回统一 JSON 结构 {code, reason, trace_id}。"""
    res = client.get("/nope")
    assert res.status_code == 404
    body = res.json()
    assert set(body) == {"code", "reason", "trace_id"}
    assert body["code"] == "NOT_FOUND"
    assert isinstance(body["reason"], str)
    assert body["trace_id"]


def test_error_code_enum_values() -> None:
    assert ErrorCode.INTERNAL.value == "INTERNAL_ERROR"
    assert ErrorCode.VALIDATION.value == "VALIDATION_ERROR"
    assert ErrorCode.PERMISSION.value == "PERMISSION_DENIED"
    assert ErrorCode.NOT_FOUND.value == "NOT_FOUND"
    assert ErrorCode.PLUGIN_NOT_FOUND.value == "PLUGIN_NOT_FOUND"
    assert ErrorCode.CONFIG_ERROR.value == "CONFIG_ERROR"
    assert ErrorCode.UPSTREAM_TIMEOUT.value == "UPSTREAM_TIMEOUT"


def test_app_exception_fields() -> None:
    exc = AppException(code=ErrorCode.CONFIG_ERROR, reason="bad config", trace_id="t-1")
    assert exc.code is ErrorCode.CONFIG_ERROR
    assert exc.reason == "bad config"
    assert exc.trace_id == "t-1"
    assert str(exc) == "bad config"


def test_app_exception_no_trace_id_default() -> None:
    exc = AppException(code=ErrorCode.NOT_FOUND, reason="missing")
    assert exc.trace_id is None
