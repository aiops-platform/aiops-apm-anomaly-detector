"""UC-7.2：``/v1/audit`` 轮次审计查询（检测轮次 + 被抑制信号摊平）。

普通鉴权（非 admin）——审计是运维查询；``get_tenant_id`` 租户隔离。
``GET /v1/audit/suppressed`` 从 ``detection_round.timeline`` 的 suppressed 步骤 details 摊平
（V1 无独立 suppressed_detail 表，审计数据落在 timeline JSON）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from .deps import get_tenant_id

router = APIRouter(prefix="/v1/audit", tags=["audit"])


@router.get("/rounds")
async def list_rounds(
    request: Request,
    domain: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """按租户列检测轮次，可选按 domain / status 过滤，started_at 倒序。"""
    tenant = get_tenant_id(request)
    items = await request.app.state.storage.rounds.list_rounds(
        tenant, domain=domain, status=status, limit=limit, offset=offset
    )
    return {"items": items, "count": len(items)}


@router.get("/suppressed")
async def list_suppressed(request: Request, service: str | None = None) -> dict:
    """从轮次 timeline 摊平被抑制信号（可选按 service 过滤）。"""
    tenant = get_tenant_id(request)
    max_rounds = int(getattr(request.app.state.settings, "round_retention_rounds", 1000))
    rounds = await request.app.state.storage.rounds.list_rounds(tenant, limit=max_rounds)
    rows: list[dict] = []
    for r in rounds:
        for step in r.get("timeline", []):
            if step.get("step") != "suppressed":
                continue
            for d in step.get("details", []):
                if service is not None and d.get("service") != service:
                    continue
                rows.append(
                    {
                        "round_id": r["round_id"],
                        "time": r.get("started_at"),
                        "signal": d.get("signal"),
                        "service": d.get("service"),
                        "suppressor": d.get("suppressor"),
                        "reason": d.get("reason"),
                    }
                )
    return {"items": rows, "count": len(rows)}
