"""监控端点管理 API（M3）：``/v1/monitors`` CRUD + 连通性测试。

所有端点从 ``X-Tenant-Id`` 头解析租户；创建/更新先过出站安全网关校验
（``validate_url`` + ``validate_headers``），SSRF / 明文凭据在落库前被拒。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..collectors import CollectContext, SharedHttpClient, collector_for
from ..collectors._gateway import OutboundGateway
from ..exceptions import AppException, ErrorCode
from .deps import get_tenant_id

router = APIRouter(prefix="/v1/monitors", tags=["monitors"])

_SAMPLE_LIMIT = 20


def _store(request: Request):
    """取当前应用的 MonitorTargetStore。"""
    return request.app.state.storage.monitor_targets


@router.post("", status_code=201)
async def create_monitor(request: Request, body: dict) -> dict:
    """新建监控端点（UC-3.1）：先过安全网关，再落库，返回 target_id。"""
    tenant = get_tenant_id(request)
    sc = body.get("source_config", {})
    OutboundGateway.validate_url(sc.get("url", ""))
    OutboundGateway.validate_headers(sc.get("headers", {}))
    created = await _store(request).create(tenant, body)
    return {"target_id": created["target_id"]}


@router.get("")
async def list_monitors(request: Request, service: str | None = None, signal_type: str | None = None) -> dict:
    """列出监控端点，可按 service / signal_type 过滤。"""
    tenant = get_tenant_id(request)
    items = await _store(request).list(tenant, service=service, signal_type=signal_type)
    return {"items": items}


@router.get("/{target_id}")
async def get_monitor(request: Request, target_id: str) -> dict:
    """端点详情。"""
    tenant = get_tenant_id(request)
    target = await _store(request).get(tenant, target_id)
    if target is None:
        raise AppException(ErrorCode.NOT_FOUND, f"monitor target not found: {target_id}")
    return target


@router.put("/{target_id}")
async def update_monitor(request: Request, target_id: str, body: dict) -> dict:
    """更新端点；若变更 source_config 则重新过安全网关。"""
    tenant = get_tenant_id(request)
    sc = body.get("source_config")
    if sc:
        OutboundGateway.validate_url(sc.get("url", ""))
        OutboundGateway.validate_headers(sc.get("headers", {}))
    updated = await _store(request).update(tenant, target_id, body)
    if updated is None:
        raise AppException(ErrorCode.NOT_FOUND, f"monitor target not found: {target_id}")
    return updated


@router.delete("/{target_id}", status_code=204)
async def delete_monitor(request: Request, target_id: str) -> None:
    """软删端点（enabled=0）。"""
    tenant = get_tenant_id(request)
    await _store(request).delete(tenant, target_id)


@router.post("/{target_id}/test")
async def test_monitor(request: Request, target_id: str) -> dict:
    """测试采集连通性（UC-3.2）：一次采集，不写水位线/快照。

    网关校验错误走 ``AppException``（4xx）；上游失败（超时/字段缺失/HTTP 错误）
    返回 ``status="error"`` 的结构化结果（200）。FastAPI 对返回 dict 走
    ``jsonable_encoder``，datetime 等类型自动序列化。
    """
    tenant = get_tenant_id(request)
    target = await _store(request).get(tenant, target_id)
    if target is None:
        raise AppException(ErrorCode.NOT_FOUND, f"monitor target not found: {target_id}")

    http: SharedHttpClient | None = getattr(request.app.state, "http_client", None)
    collector = collector_for(target, http=http)
    ctx = CollectContext(tenant_id=tenant)  # watermark/snapshot=None → 不写库

    try:
        signals = await collector.collect(ctx, target)
    except AppException:
        raise
    except Exception as exc:
        return {
            "target_id": target_id,
            "status": "error",
            "reason": str(exc),
            "signal_count": 0,
            "signals": [],
        }
    return {
        "target_id": target_id,
        "status": "ok",
        "signal_count": len(signals),
        "signals": [s.model_dump() for s in signals[:_SAMPLE_LIMIT]],
    }
