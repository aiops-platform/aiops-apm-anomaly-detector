"""UC-6.4：``/v1/problems`` 问题单查询与手动关闭。M7（UC-7.6）resolve 支持误报回写。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..exceptions import AppException, ErrorCode
from ..metrics import update_fpr_gauge
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
async def resolve_problem(request: Request, record_id: str, body: dict | None = None) -> dict:
    """手动关闭问题单（reason=manual）。

    M7（UC-7.6）：可选 body ``{"false_positive": true}``——为真时把该单 ``group_key``
    记为一次误报，写回 ``fpr_table``（total+1，fpr 重算）并更新 ``aiops_false_positive_rate`` Gauge。
    body 缺省 / 为假 → 记为一次有效判定（非误报）。
    """
    false_positive = bool((body or {}).get("false_positive", False))
    tenant = get_tenant_id(request)
    storage = request.app.state.storage
    rec = await storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")

    group_key = rec.get("group_key")
    if group_key:
        await storage.dynamic_config.write_fpr(tenant, group_key, false_positive=false_positive)
        fpr_data = await storage.dynamic_config.load_fpr(tenant)
        update_fpr_gauge(
            tenant,
            rec.get("domain", "application"),
            rec.get("service", "unknown"),
            fpr_data,
        )

    await storage.records.resolve(tenant, record_id, reason="manual")
    return {
        "record_id": record_id,
        "state": "resolved",
        "false_positive_recorded": bool(group_key),
    }
