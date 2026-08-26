"""UC-6.4：``/v1/problems`` 问题单查询与手动关闭。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..exceptions import AppException, ErrorCode
from .deps import get_tenant_id

router = APIRouter(prefix="/v1/problems", tags=["problems"])


@router.get("")
async def list_problems(
    request: Request,
    state: str | None = None,
    service: str | None = None,
    severity: str | None = None,
    limit: int = 50,
) -> dict:
    """按租户列问题单，可选 state / service / severity 过滤，detected_at 倒序。"""
    tenant = get_tenant_id(request)
    items = await request.app.state.storage.records.list(
        tenant, state=state, service=service, severity=severity, limit=limit
    )
    return {"items": items}


@router.get("/{record_id}")
async def get_problem(request: Request, record_id: str) -> dict:
    """单条问题单详情。"""
    tenant = get_tenant_id(request)
    rec = await request.app.state.storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")
    return rec


@router.post("/{record_id}/resolve")
async def resolve_problem(request: Request, record_id: str) -> dict:
    """手动关闭问题单（reason=manual）。"""
    tenant = get_tenant_id(request)
    rec = await request.app.state.storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")
    await request.app.state.storage.records.resolve(tenant, record_id, reason="manual")
    return {"record_id": record_id, "state": "resolved"}
