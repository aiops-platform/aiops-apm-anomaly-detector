"""UC-3.7 SSRF 拦截 / UC-3.8 Secret 引用解析：``OutboundGateway``。"""

import pytest

from aiops_apm.collectors._gateway import OutboundGateway
from aiops_apm.exceptions import AppException, ErrorCode

# ---- validate_url ----

def test_validate_url_allows_public_http_https(monkeypatch):
    # hostname 走 DNS 二次校验：monkeypatch 解析为公网 IP → 放行
    monkeypatch.setattr("aiops_apm.collectors._gateway._resolve_ips", lambda host: ["93.184.216.34"])
    url = "https://prometheus.example.com:9090/api/v1/query"
    assert OutboundGateway.validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # 云元数据
        "http://127.0.0.1:9200/logs/_search",
        "http://10.0.0.1/metrics",
        "http://192.168.1.1:8080/logs",
        "http://[::1]:8080/",
    ],
)
def test_validate_url_rejects_private_networks(url):
    with pytest.raises(AppException) as excinfo:
        OutboundGateway.validate_url(url)
    assert excinfo.value.code == ErrorCode.VALIDATION
    assert "blocked network" in excinfo.value.reason


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9200/logs/_search",  # IPv4 回环字面量
        "http://[::1]:8080/",  # IPv6 回环字面量
        "http://localhost:9200/logs/_search",  # hostname 解析到回环
    ],
)
def test_validate_url_allows_loopback_when_enabled(monkeypatch, url):
    # 开启回环放行后，回环地址不再拦截；结束还原默认（防串扰）
    monkeypatch.setattr("aiops_apm.collectors._gateway._resolve_ips", lambda host: ["127.0.0.1", "::1"])
    OutboundGateway.set_allow_loopback(True)
    try:
        assert OutboundGateway.validate_url(url) == url
    finally:
        OutboundGateway.set_allow_loopback(False)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/metrics",  # 内网
        "http://192.168.1.1:8080/logs",
        "http://169.254.169.254/latest/meta-data/",  # 云元数据
    ],
)
def test_validate_url_still_blocks_private_with_loopback_enabled(url):
    # 即使开启回环放行，内网/云元数据仍必须拦截
    OutboundGateway.set_allow_loopback(True)
    try:
        with pytest.raises(AppException) as excinfo:
            OutboundGateway.validate_url(url)
        assert "blocked network" in excinfo.value.reason
    finally:
        OutboundGateway.set_allow_loopback(False)


def test_validate_url_rejects_disallowed_scheme():
    with pytest.raises(AppException) as excinfo:
        OutboundGateway.validate_url("file:///etc/passwd")
    assert excinfo.value.code == ErrorCode.VALIDATION
    assert "scheme not allowed" in excinfo.value.reason


# ---- validate_headers ----

def test_validate_headers_accepts_secret_ref_with_bearer_prefix():
    headers = {"Authorization": "Bearer ${env:ORDER_TOKEN}"}
    assert OutboundGateway.validate_headers(headers) == headers


def test_validate_headers_accepts_bare_secret_ref():
    headers = {"X-API-Key": "${env:API_KEY}"}
    assert OutboundGateway.validate_headers(headers) == headers


def test_validate_headers_rejects_plaintext_bearer():
    with pytest.raises(AppException) as excinfo:
        OutboundGateway.validate_headers({"Authorization": "Bearer abc123"})
    assert excinfo.value.code == ErrorCode.VALIDATION
    assert "plaintext credential" in excinfo.value.reason


def test_validate_headers_rejects_auth_header_without_secret_ref():
    with pytest.raises(AppException) as excinfo:
        OutboundGateway.validate_headers({"Authorization": "my-fixed-token"})
    assert excinfo.value.code == ErrorCode.VALIDATION
    assert "secret reference" in excinfo.value.reason


# ---- resolve_secret ----

def test_resolve_secret_env_var(monkeypatch):
    monkeypatch.setenv("ORDER_TOKEN", "tok-123")
    assert OutboundGateway.resolve_secret("${env:ORDER_TOKEN}") == "tok-123"


def test_resolve_secret_embedded_in_bearer(monkeypatch):
    monkeypatch.setenv("ORDER_TOKEN", "tok-123")
    assert OutboundGateway.resolve_secret("Bearer ${env:ORDER_TOKEN}") == "Bearer tok-123"


def test_resolve_secret_missing_env_returns_empty():
    assert OutboundGateway.resolve_secret("${env:NOT_SET_VAR_XYZ}") == ""


def test_resolve_secret_vault_placeholder():
    assert OutboundGateway.resolve_secret("${vault:secret/order#token}") == ""


def test_resolve_secret_plain_string_unchanged():
    assert OutboundGateway.resolve_secret("hello") == "hello"
