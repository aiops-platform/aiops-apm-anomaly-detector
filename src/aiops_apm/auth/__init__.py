"""鉴权（M6 UC-6.8）：``Principal`` + ``get_principal``。

``settings.api_keys`` 非空才挂 ``AuthMiddleware``；未配置 = 放行（既有 API 测试零改动）。
无中间件时 ``get_principal`` 返回匿名 admin（dev 兼容）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Request

from aiops_apm.exceptions import AppException, ErrorCode


@dataclass
class Principal:
    """当前请求主体。``tenants=["*"]`` 表全租户（master key 即 admin）。"""

    api_key: str = "anonymous"
    tenants: list[str] = field(default_factory=lambda: ["*"])
    is_admin: bool = True


def get_principal(request: Request) -> Principal:
    """取请求主体；中间件未装（或未写 state）→ 匿名 admin。"""
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return principal
    return Principal()


def require_admin(principal: Principal) -> None:
    """admin 路由守卫（plugins reload / config PUT / alerts run）。"""
    if not principal.is_admin:
        raise AppException(ErrorCode.PERMISSION, "admin privileges required")
