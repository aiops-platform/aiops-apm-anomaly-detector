"""注入式共享 HTTP 客户端（连接池、超时、大小限制）。

- 超时用 ``settings.outbound_timeout_sec``；禁止自动跳转（``follow_redirects=False``）。
- ``httpx.AsyncClient`` 无 ``max_content_length`` 参数，大小限制在 ``request()`` 返回后手动检查。
- 测试可注入 ``transport``（如 ``httpx.MockTransport``）。
"""

from __future__ import annotations

from typing import Any

import httpx

from ..exceptions import AppException, ErrorCode
from ..settings import Settings


class SharedHttpClient:
    """共享出站 HTTP 客户端。"""

    def __init__(self, settings: Settings, *, transport: Any = None, max_content_length: int | None = None) -> None:
        self._settings = settings
        self._max_content_length = max_content_length if max_content_length is not None else settings.outbound_max_body_bytes
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.outbound_timeout_sec),
            limits=httpx.Limits(max_connections=50),
            follow_redirects=False,  # 禁止自动跳转
            transport=transport,
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """发起请求并做响应体大小限制检查。"""
        resp = await self._client.request(method, url, **kwargs)
        if len(resp.content) > self._max_content_length:
            raise AppException(ErrorCode.VALIDATION, "response too large")
        return resp

    async def aclose(self) -> None:
        await self._client.aclose()
