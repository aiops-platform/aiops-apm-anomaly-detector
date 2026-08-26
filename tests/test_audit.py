"""UC-7.6 SecurityAudit：四类安全事件发结构化日志 + 不记明文凭据。"""

import logging

from aiops_apm.audit import SecurityAudit, set_audit_enabled

LOGGER = "aiops_apm.audit"


def test_log_auth_event(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        SecurityAudit.log_auth_event("t1", "request", "deny", detail="missing_or_invalid_key")
    recs = [r for r in caplog.records if r.name == LOGGER]
    assert len(recs) == 1
    assert "tenant=t1" in recs[0].getMessage()
    assert "outcome=deny" in recs[0].getMessage()


def test_log_gateway_event(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        SecurityAudit.log_gateway_event("http://127.0.0.1:9200/logs/_search?x=1", True, "blocked network")
    msg = caplog.records[-1].getMessage()
    # uri 只留 host:port，去掉 query 避免泄密
    assert "http://127.0.0.1:9200" in msg
    assert "_search" not in msg
    assert "blocked=True" in msg  # bool 走 %s 格式化为 "True"


def test_log_plugin_event(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        SecurityAudit.log_plugin_event("my_detector", "load", "failed", detail="ImportError: no module")
    msg = caplog.records[-1].getMessage()
    assert "name=my_detector" in msg
    assert "outcome=failed" in msg


def test_log_config_event(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        SecurityAudit.log_config_event("application", "put", "success", detail="version=2")
    msg = caplog.records[-1].getMessage()
    assert "domain=application" in msg
    assert "action=put" in msg


def test_log_round_event(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        SecurityAudit.log_round_event("t1", "R-0001", "application", "failed", detail="TimeoutError: ...")
    msg = caplog.records[-1].getMessage()
    assert "round_id=R-0001" in msg
    assert "status=failed" in msg


def test_auth_detail_never_contains_plaintext_key(caplog) -> None:
    # 审计 detail 只允许固定枚举，密钥明文不得出现在日志
    with caplog.at_level(logging.INFO, logger=LOGGER):
        SecurityAudit.log_auth_event("t1", "request", "deny", detail="missing_or_invalid_key")
    msg = caplog.records[-1].getMessage()
    assert "secret-key-abc" not in msg


def test_audit_disabled_silent(caplog) -> None:
    set_audit_enabled(False)
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            SecurityAudit.log_auth_event("t1", "request", "deny", detail="missing_or_invalid_key")
        assert [r for r in caplog.records if r.name == LOGGER] == []
    finally:
        set_audit_enabled(True)
