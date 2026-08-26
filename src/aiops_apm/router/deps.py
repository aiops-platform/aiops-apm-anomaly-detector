"""路由公共依赖：租户解析。"""

from __future__ import annotations

from fastapi import Request


def get_tenant_id(request: Request) -> str:
    """从 ``X-Tenant-Id`` 请求头解析租户（默认 ``default``）。

    多租户约定（CLAUDE.md）：服务端解析请求头，**绝不信任请求体中的 tenant_id**。
    """
    return request.headers.get("X-Tenant-Id", "default")
