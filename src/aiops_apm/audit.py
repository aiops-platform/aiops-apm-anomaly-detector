"""安全审计日志（UC-7.6）：鉴权 / 出站网关 / 插件 / 配置 的结构化日志。

设计决策（用户已确认「完整 M7」范围）：
- **不落库**（日志即审计），输出到 ``logging.getLogger("aiops_apm.audit")``，
  由进程日志收集（如 Docker 的 stdout / 日志采集 agent）。
- **不记明文凭据**：API key 只记 sha256 前缀；URI 只记 host:port（不含 query/secret）。
- ``audit_enabled`` 开关：``_app`` lifespan 启动时 ``set_audit_enabled(settings.audit_enabled)``。
"""

from __future__ import annotations

import hashlib
import logging

_logger = logging.getLogger("aiops_apm.audit")

_audit_enabled = True


def set_audit_enabled(enabled: bool) -> None:
    """全局审计开关（``APM_AUDIT_ENABLED``）。"""
    global _audit_enabled
    _audit_enabled = bool(enabled)


def _key_prefix(api_key: str) -> str:
    """API key 只留 sha256 前 8 位，避免明文凭据进日志。"""
    return hashlib.sha256(api_key.encode()).hexdigest()[:8]


def _uri_short(uri: str) -> str:
    """只留 scheme://host:port（含 IPv6 方括号），去掉 query/path 避免泄密。"""
    from urllib.parse import urlparse

    p = urlparse(uri)
    host = p.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{p.port}" if p.port else ""
    return f"{p.scheme}://{host}{port}"


class SecurityAudit:
    """四类安全审计静态方法（+ 轮次审计），全部走同一 logger。"""

    @staticmethod
    def log_auth_event(tenant: str, action: str, outcome: str, detail: str | None = None) -> None:
        """鉴权事件（allow/deny）。``detail`` 只允许固定枚举，不记 key 明文。"""
        if not _audit_enabled:
            return
        _logger.info(
            "auth tenant=%s action=%s outcome=%s detail=%s",
            tenant or "-", action, outcome, detail or "-",
        )

    @staticmethod
    def log_gateway_event(uri: str, blocked: bool, reason: str) -> None:
        """出站请求安全事件（SSRF / scheme / secret 校验拒绝）。"""
        if not _audit_enabled:
            return
        _logger.info(
            "gateway uri=%s blocked=%s reason=%s",
            _uri_short(uri), blocked, reason or "-",
        )

    @staticmethod
    def log_plugin_event(plugin_name: str, action: str, outcome: str, detail: str | None = None) -> None:
        """插件加载事件（load:success / load:failed）。"""
        if not _audit_enabled:
            return
        _logger.info(
            "plugin name=%s action=%s outcome=%s detail=%s",
            plugin_name, action, outcome, detail or "-",
        )

    @staticmethod
    def log_config_event(domain: str, action: str, outcome: str, detail: str | None = None) -> None:
        """配置事件（PUT / reload）。"""
        if not _audit_enabled:
            return
        _logger.info(
            "config domain=%s action=%s outcome=%s detail=%s",
            domain or "-", action, outcome, detail or "-",
        )

    @staticmethod
    def log_round_event(tenant: str, round_id: str, domain: str, status: str, detail: str | None = None) -> None:
        """检测轮次事件（成功 / 降级 / 失败）。"""
        if not _audit_enabled:
            return
        _logger.info(
            "round tenant=%s round_id=%s domain=%s status=%s detail=%s",
            tenant, round_id, domain, status, detail or "-",
        )
