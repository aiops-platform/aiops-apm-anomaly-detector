"""出站安全网关：所有采集器发出的 HTTP 请求必须先过此关。

职责（P0#6 SSRF / secret 在此落地）：
- ``validate_url``：scheme 白名单 + 私网/云元数据地址拦截（SSRF）。
- ``validate_headers``：拒绝明文凭据；``authorization`` / ``x-api-key`` 必须用 ``${env:X}`` / ``${vault:path#key}`` 引用。
- ``resolve_secret``：把 secret 引用解析为实际值（env 缺失返回空串；vault 为占位）。

无状态（classmethod），不依赖 DB / 外部服务。域名校验只拦 IP 字面量；
DNS 解析后的二次校验留 M7 安全加固。
"""

from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlparse

from ..exceptions import AppException, ErrorCode


class OutboundGateway:
    """出站安全网关。"""

    ALLOWED_SCHEMES = {"http", "https"}
    BLOCKED_NETWORKS = [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("169.254.0.0/16"),  # 云元数据（如 169.254.169.254）
        ipaddress.ip_network("::1/128"),
    ]
    SECRET_REF_PATTERN = re.compile(r"\$\{(env|vault):[^}]+\}")
    PLAINTEXT_CRED_PATTERN = re.compile(
        r"(Bearer\s+[A-Za-z0-9\-_\.]+|AKIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,})",
        re.IGNORECASE,
    )
    # 需 secret 引用的 header（大小写不敏感）
    SECRET_HEADERS = ("authorization", "x-api-key")

    @classmethod
    def validate_url(cls, url: str) -> str:
        """校验 URL 安全性；通过则原样返回，否则抛 ``AppException(VALIDATION, ...)``。"""
        parsed = urlparse(url)
        if parsed.scheme not in cls.ALLOWED_SCHEMES:
            raise AppException(ErrorCode.VALIDATION, f"scheme not allowed: {parsed.scheme}")
        hostname = parsed.hostname
        if hostname:
            try:
                ip = ipaddress.ip_address(hostname)
                for net in cls.BLOCKED_NETWORKS:
                    if ip in net:
                        raise AppException(ErrorCode.VALIDATION, f"blocked network: {ip}")
            except ValueError:
                pass  # 域名，后续 DNS 解析后再次校验（M7）
        return url

    @classmethod
    def validate_headers(cls, headers: dict) -> dict:
        """校验 headers 的 secret 引用；通过则原样返回，否则抛 ``AppException(VALIDATION, ...)``。"""
        for k, v in headers.items():
            if not isinstance(v, str):
                continue
            if cls.PLAINTEXT_CRED_PATTERN.search(v):
                raise AppException(ErrorCode.VALIDATION, f"plaintext credential in header: {k}")
            # authorization/x-api-key 的值必须包含 secret 引用（支持 "Bearer ${env:X}" 形式）
            if k.lower() in cls.SECRET_HEADERS and not cls.SECRET_REF_PATTERN.search(v):
                raise AppException(ErrorCode.VALIDATION, f"header must use secret reference: {k}")
        return headers

    @classmethod
    def resolve_secret(cls, ref: str) -> str:
        """解析 ``${env:X}`` / ``${vault:path#key}`` 引用（支持嵌入字符串，如 ``Bearer ${env:X}``）。

        env 变量不存在时返回空串；vault 为占位（M7 接入密钥管理系统）。
        """
        if not isinstance(ref, str):
            return ref
        return re.sub(r"\$\{(env|vault):([^}]+)\}", lambda m: cls._resolve_ref(m.group(1), m.group(2)), ref)

    @classmethod
    def _resolve_ref(cls, backend: str, key: str) -> str:
        if backend == "env":
            return os.environ.get(key, "")
        return ""  # vault 占位：M7 接入真实密钥管理
