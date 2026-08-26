"""UC-6.10：``/v1/maintenance-windows`` 维护窗口 CRUD。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..exceptions import AppException, ErrorCode
from .deps import get_tenant_id

router = APIRouter(prefix="/v1/maintenance-windows", tags=["maintenance"])


def _store(request: Request):
    return request.app.state.storage.dynamic_config


@router.post("", status_code=201)
async def create_window(request: Request, body: dict) -> dict:
    """新建维护窗口。"""
    tenant = get_tenant_id(request)
    return await _store(request).create_maintenance_window(tenant, body)


@router.get("")
async def list_windows(request: Request, service: str | None = None) -> dict:
    """列出维护窗口，可按 service 过滤。"""
    tenant = get_tenant_id(request)
    items = await _store(request).list_maintenance_windows(tenant, service=service)
    return {"items": items}


@router.put("/{window_id}")
async def update_window(request: Request, window_id: int, body: dict) -> dict:
    """更新维护窗口字段。"""
    tenant = get_tenant_id(request)
    row = await _store(request).update_maintenance_window(tenant, window_id, body)
    if row is None:
        raise AppException(ErrorCode.NOT_FOUND, f"maintenance window not found: {window_id}")
    return row


@router.delete("/{window_id}", status_code=204)
async def delete_window(request: Request, window_id: int) -> None:
    """删除维护窗口。"""
    tenant = get_tenant_id(request)
    await _store(request).delete_maintenance_window(tenant, window_id)
