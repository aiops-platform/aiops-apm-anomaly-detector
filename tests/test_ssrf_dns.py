"""UC-7.6 SSRF DNS 二次校验：hostname 解析命中私网拒绝 / 公网放行 / 解析失败 fail-closed。"""

import socket

import pytest

from aiops_apm.collectors._gateway import OutboundGateway, _resolve_ips
from aiops_apm.exceptions import AppException, ErrorCode


def test_resolve_ips_returns_unique_ips() -> None:
    # 真实解析（公网域名）不 mock：只断言返回非空 IP 列表结构
    ips = _resolve_ips("example.com")
    assert isinstance(ips, list)
    assert len(ips) >= 1
    assert all(":" in ip or "." in ip for ip in ips)


def test_dns_resolves_private_ip_rejected(monkeypatch) -> None:
    monkeypatch.setattr("aiops_apm.collectors._gateway._resolve_ips", lambda host: ["127.0.0.1"])
    with pytest.raises(AppException) as excinfo:
        OutboundGateway.validate_url("http://internal.example.com:9200/_search")
    assert excinfo.value.code == ErrorCode.VALIDATION
    assert "blocked network" in excinfo.value.reason


def test_dns_resolves_ipv6_loopback_rejected(monkeypatch) -> None:
    monkeypatch.setattr("aiops_apm.collectors._gateway._resolve_ips", lambda host: ["::1"])
    with pytest.raises(AppException, match="blocked network"):
        OutboundGateway.validate_url("http://ipv6.internal.example.com/")


def test_dns_resolves_public_ip_allowed(monkeypatch) -> None:
    monkeypatch.setattr("aiops_apm.collectors._gateway._resolve_ips", lambda host: ["93.184.216.34"])
    url = "https://prometheus.example.com:9090/api/v1/query"
    assert OutboundGateway.validate_url(url) == url


def test_dns_resolution_failure_fail_closed(monkeypatch) -> None:
    def _gaierror(host: str) -> list[str]:
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr("aiops_apm.collectors._gateway._resolve_ips", _gaierror)
    with pytest.raises(AppException) as excinfo:
        OutboundGateway.validate_url("https://unresolvable.example.com/")
    assert excinfo.value.code == ErrorCode.VALIDATION
    assert "dns resolution failed" in excinfo.value.reason


def test_ip_literal_interception_does_not_regress(monkeypatch) -> None:
    # IP 字面量分支在 DNS 前执行，不触发 _resolve_ips
    called = []

    def _spy(host: str) -> list[str]:
        called.append(host)
        return ["93.184.216.34"]

    monkeypatch.setattr("aiops_apm.collectors._gateway._resolve_ips", _spy)
    with pytest.raises(AppException, match="blocked network"):
        OutboundGateway.validate_url("http://192.168.1.1:8080/metrics")
    assert called == []  # 字面量拦截未走 DNS
