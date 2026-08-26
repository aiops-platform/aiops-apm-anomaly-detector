"""UC-6.6：``/v1/config`` 配置热加载与域规则读写。M7（UC-7.3）写入侧校验 + 配置审计日志。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from ..audit import SecurityAudit
from ..auth import get_principal, require_admin
from ..config.loader import DomainConfigLoader
from ..config.validator import validate_domain_config
from ..exceptions import AppException, ErrorCode
from ..models.config import DomainConfig
from .deps import get_tenant_id

router = APIRouter(prefix="/v1/config", tags=["config"])


def _storage(request: Request):
    return request.app.state.storage


@router.post("/reload")
async def reload_config(request: Request) -> dict:
    """重载插件 registry（放线程避免阻塞事件循环），返回更新后的插件列表。"""
    require_admin(get_principal(request))
    state = request.app.state
    reg = getattr(state, "registry", None)
    if reg is None:
        raise AppException(ErrorCode.INTERNAL, "plugin registry not loaded")
    try:
        await asyncio.to_thread(
            reg.reload,
            http=state.http_client,
            pool=getattr(state.storage, "pool", None),
            settings=state.settings,
        )
    except Exception as exc:  # noqa: BLE001 -- 审计失败事件后仍向上抛
        SecurityAudit.log_config_event("*", "reload", "failed", detail=f"{type(exc).__name__}: {exc}")
        raise
    SecurityAudit.log_config_event("*", "reload", "success")
    return {"plugins": reg.list()}


@router.get("/{domain}")
async def get_domain_config(request: Request, domain: str) -> dict:
    """读该租户某域的检测规则。"""
    tenant = get_tenant_id(request)
    rows = await DomainConfigLoader(_storage(request).domain_configs).load(tenant)
    for r in rows:
        if r["domain"] == domain:
            return {"domain": domain, "config": r["config"], "version": r["version"]}
    raise AppException(ErrorCode.NOT_FOUND, f"no domain config for domain={domain!r}")


@router.put("/{domain}")
async def put_domain_config(request: Request, domain: str, body: dict) -> dict:
    """更新该租户某域的检测规则（admin）。

    M7（UC-7.3）：``DomainConfig.model_validate`` 基础校验后接 ``validate_domain_config``
    表驱动结构校验（detector/suppressor 参数），非法抛 ``ConfigValidationError`` → HTTP 400。
    """
    require_admin(get_principal(request))
    tenant = get_tenant_id(request)
    state = request.app.state
    cfg = DomainConfig.model_validate(body)
    reg = getattr(state, "registry", None)
    if reg is not None:
        validate_domain_config(cfg, reg)
    version = await _storage(request).domain_configs.upsert(tenant, domain, cfg)
    SecurityAudit.log_config_event(domain, "put", "success", detail=f"version={version}")
    return {"domain": domain, "version": version}
