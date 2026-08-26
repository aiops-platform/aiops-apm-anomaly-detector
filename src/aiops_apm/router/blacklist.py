"""UC-6.11：``/v1/blacklist`` 黑名单 CRUD（admin 全量视图，含 disabled 行）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..exceptions import AppException, ErrorCode
from .deps import get_tenant_id

router = APIRouter(prefix="/v1/blacklist", tags=["blacklist"])


def _store(request: Request):
    return request.app.state.storage.dynamic_config


@router.post("", status_code=201)
async def create_entry(request: Request, body: dict) -> dict:
    """新建黑名单条目。"""
    tenant = get_tenant_id(request)
    return await _store(request).create_blacklist(tenant, body)


@router.get("")
async def list_entries(request: Request) -> dict:
    """列出黑名单（含 disabled 行）。"""
    tenant = get_tenant_id(request)
    items = await _store(request).list_blacklist(tenant)
    return {"items": items}


@router.put("/{entry_id}")
async def update_entry(request: Request, entry_id: int, body: dict) -> dict:
    """更新黑名单条目（可启停）。"""
    tenant = get_tenant_id(request)
    row = await _store(request).update_blacklist(tenant, entry_id, body)
    if row is None:
        raise AppException(ErrorCode.NOT_FOUND, f"blacklist entry not found: {entry_id}")
    return row


@router.delete("/{entry_id}", status_code=204)
async def delete_entry(request: Request, entry_id: int) -> None:
    """删除黑名单条目。"""
    tenant = get_tenant_id(request)
    await _store(request).delete_blacklist(tenant, entry_id)
