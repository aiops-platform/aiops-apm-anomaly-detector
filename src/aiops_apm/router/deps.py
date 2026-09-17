"""路由公共依赖：租户解析 + 主体透传。"""

from __future__ import annotations

from fastapi import Request

from ..auth import Principal, get_principal

__all__ = ["get_tenant_id", "get_principal", "Principal"]


def get_tenant_id(request: Request) -> str:
    """从 ``X-Tenant-Id`` 请求头解析租户（默认 ``default``），统一归一为小写。

    多租户约定（CLAUDE.md）：服务端解析请求头，**绝不信任请求体中的 tenant_id**。

    大小写归一是 M8（PG 化）引入的：MySQL 的 ``utf8mb4_unicode_ci`` 排序规则大小写**不敏感**，
    ``X-Tenant-Id: Default`` 能查到 ``default`` 的数据；PG 默认排序规则大小写**敏感**，同样的
    请求会返回空列表——表现为"这个租户的数据不见了"。在入口归一，使两种大小写等价。
    """
    return request.headers.get("X-Tenant-Id", "default").strip().lower()
