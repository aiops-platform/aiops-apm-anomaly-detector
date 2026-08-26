"""AuthMiddleware：``settings.api_keys`` 非空才安装（配置了才强制，用户确认）。

流程：解析 ``Authorization: Bearer <key>`` → 查 ``api_keys`` 得租户 scope
（``"*"`` 表全租户）→ 无/非法 key → 401；``X-Tenant-Id`` 超 scope → 403；
通过则把 ``Principal`` 写入 ``request.state.principal``。
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ..exceptions import ErrorCode
from . import Principal


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, api_keys: dict[str, str]) -> None:
        super().__init__(app)
        self._api_keys = api_keys

    @staticmethod
    def _scope(raw: str) -> list[str]:
        if raw.strip() == "*":
            return ["*"]
        return [t.strip() for t in raw.split(",") if t.strip()]

    async def dispatch(self, request, call_next):
        auth = request.headers.get("Authorization", "")
        key = auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""
        scope_raw = self._api_keys.get(key) if key else None
        if scope_raw is None:
            return JSONResponse(
                status_code=401,
                content={
                    "code": "UNAUTHORIZED",
                    "reason": "missing or invalid API key",
                    "trace_id": uuid.uuid4().hex,
                },
            )
        tenants = self._scope(scope_raw)
        requested = request.headers.get("X-Tenant-Id", "default")
        if "*" not in tenants and requested not in tenants:
            return JSONResponse(
                status_code=403,
                content={
                    "code": ErrorCode.PERMISSION.value,
                    "reason": f"tenant {requested!r} not in scope of API key",
                    "trace_id": uuid.uuid4().hex,
                },
            )
        request.state.principal = Principal(
            api_key=key,
            tenants=tenants,
            is_admin=("*" in tenants),
        )
        return await call_next(request)
