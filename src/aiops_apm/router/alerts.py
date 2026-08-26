"""UC-6.3：``POST /v1/alerts/run`` 手动全跑（可选 ``?domain=`` 过滤）。"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from ..auth import get_principal, require_admin
from ..poller import run_round
from .deps import get_tenant_id

router = APIRouter(prefix="/v1/alerts", tags=["alerts"])


@router.post("/run")
async def run_alerts(request: Request, domain: str | None = None) -> dict:
    """对该租户全部启用端点跑一轮采集+漏斗，按 (domain) 分组汇总。"""
    require_admin(get_principal(request))
    state = request.app.state
    storage = state.storage
    tenant = get_tenant_id(request)

    targets = await storage.monitor_targets.load_all_targets(tenant)
    if domain is not None:
        targets = [t for t in targets if t.get("domain") == domain]

    groups: dict[str, list] = defaultdict(list)
    for t in targets:
        groups[str(t.get("domain", "application"))].append(t)

    now = datetime.now(timezone.utc)
    rounds: list[dict] = []
    total = 0
    for d, ts in groups.items():
        result = await run_round(
            registry=state.registry,
            storage=storage,
            tenant_id=tenant,
            domain=d,
            targets=ts,
            now=now,
            http=state.http_client,
            settings=state.settings,
        )
        rounds.append(
            {
                "domain": d,
                "target_count": len(ts),
                "record_count": len(result.records),
                "degraded_sources": result.degraded_sources,
            }
        )
        total += len(result.records)
    return {"rounds": rounds, "total_records": total}
