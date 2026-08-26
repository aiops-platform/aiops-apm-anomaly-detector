"""API 路由：M0 探针 + M3 /v1/monitors 监控端点管理 + M4 /v1/plugins 插件管理。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .monitors import router as monitors_router
from .plugins import router as plugins_router

api_router = APIRouter()
api_router.include_router(monitors_router)
api_router.include_router(plugins_router)


@api_router.get("/health")
async def health() -> dict:
    """存活探针：进程在即返回 200。"""
    return {"status": "ok"}


@api_router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """就绪探针：检查 DB 连接与插件加载状态。

    M2 起在 lifespan 构建 app.state.storage（fail-fast，连不上 DB 直接启动失败）；
    db 反映运行时真实连接状态（memory 恒可用，mysql 走连接池探活，DB 挂了为 False）。
    M4 起在 lifespan 构建 app.state.registry（插件 registry），plugins 反映其加载状态。
    """
    state = request.app.state
    storage = getattr(state, "storage", None)
    db = False
    if storage is not None:
        db = await storage.health_check()
    checks = {
        "db": db,
        "plugins": bool(getattr(state, "registry", None)),
    }
    if not all(checks.values()):
        return JSONResponse(status_code=503, content={"code": "NOT_READY", "reason": str(checks)})
    return JSONResponse(status_code=200, content={"status": "ready", "checks": checks})
