"""出站安全网关：所有采集器发出的 HTTP 请求必须先过此关。

职责（P0#6 SSRF / secret 在此落地）：
- ``validate_url``：scheme 白名单 + 私网/云元数据地址拦截（SSRF）。
  域名先查 IP 字面量，再走 DNS 二次校验（``_resolve_ips``）——解析出的任一 IP
  命中 ``BLOCKED_NETWORKS`` 拒绝；**解析失败也拒绝**（fail-closed，防 DNS rebinding
  首查放行）。
- ``validate_headers``：拒绝明文凭据；``authorization`` / ``x-api-key`` 必须用 ``${env:X}`` / ``${vault:path#key}`` 引用。
- ``resolve_secret``：把 secret 引用解析为实际值（env 缺失返回空串；vault 为占位）。

无状态（classmethod），不依赖 DB / 外部服务。拒绝分支走 ``SecurityAudit.log_gateway_event``
（UC-7.6 安全审计；uri 只记 host:port）。
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import urlparse

from ..audit import SecurityAudit
from ..exceptions import AppException, ErrorCode


def _resolve_ips(host: str) -> list[str]:
    """解析 hostname 的 A/AAAA 记录（去重），返回 IP 列表；不可解析抛 ``socket.gaierror``。"""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    ips: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in ips:
            ips.append(addr)
    return ips


def _is_ip(value: str) -> bool:
    """判断字符串是否为合法 IP 地址（含 IPv6）。"""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


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
    def validate_url(cls, url: str, *, trace_id: str | None = None) -> str:
        """校验 URL 安全性；通过则原样返回，否则抛 ``AppException(VALIDATION, ...)``。

        - IP 字面量：直接对 ``BLOCKED_NETWORKS`` 判定。
        - hostname：``_resolve_ips`` 解析后任一 IP 命中私网 → 拒绝；
          解析失败（``socket.gaierror``）→ 拒绝（fail-closed，防 DNS rebinding）。
        """
        parsed = urlparse(url)
        if parsed.scheme not in cls.ALLOWED_SCHEMES:
            SecurityAudit.log_gateway_event(url, True, f"scheme not allowed: {parsed.scheme}")
            raise AppException(ErrorCode.VALIDATION, f"scheme not allowed: {parsed.scheme}")
        hostname = parsed.hostname
        if not hostname:
            SecurityAudit.log_gateway_event(url, True, "missing host")
            raise AppException(ErrorCode.VALIDATION, "missing host")
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            ip = None
        if ip is not None:
            cls._check_blocked(url, [ip])
        else:
            try:
                ips = _resolve_ips(hostname)
            except socket.gaierror as exc:
                SecurityAudit.log_gateway_event(url, True, f"dns resolution failed: {hostname}")
                raise AppException(ErrorCode.VALIDATION, f"dns resolution failed for {hostname}") from exc
            cls._check_blocked(url, [ipaddress.ip_address(i) for i in ips if _is_ip(i)])
        return url

    @classmethod
    def _check_blocked(cls, url: str, ips: list) -> None:
        """任一 IP 命中私网/云元数据 → 拒绝（含审计）。"""
        for ip in ips:
            for net in cls.BLOCKED_NETWORKS:
                if ip in net:
                    SecurityAudit.log_gateway_event(url, True, f"blocked network: {ip}")
                    raise AppException(ErrorCode.VALIDATION, f"blocked network: {ip}")

    @classmethod
    def validate_headers(cls, headers: dict, *, trace_id: str | None = None) -> dict:
        """校验 headers 的 secret 引用；通过则原样返回，否则抛 ``AppException(VALIDATION, ...)``。"""
        for k, v in headers.items():
            if not isinstance(v, str):
                continue
            if cls.PLAINTEXT_CRED_PATTERN.search(v):
                SecurityAudit.log_gateway_event(f"header:{k}", True, "plaintext credential")
                raise AppException(ErrorCode.VALIDATION, f"plaintext credential in header: {k}")
            # authorization/x-api-key 的值必须包含 secret 引用（支持 "Bearer ${env:X}" 形式）
            if k.lower() in cls.SECRET_HEADERS and not cls.SECRET_REF_PATTERN.search(v):
                SecurityAudit.log_gateway_event(f"header:{k}", True, "missing secret reference")
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
