"""API 路由：M0 提供 /health、/ready 两个探针。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

api_router = APIRouter()


@api_router.get("/health")
async def health() -> dict:
    """存活探针：进程在即返回 200。"""
    return {"status": "ok"}


@api_router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """就绪探针：检查 DB 连接与插件加载状态。

    M2 起在 lifespan 构建 app.state.storage（fail-fast，连不上 DB 直接启动失败）；
    db 反映运行时真实连接状态（memory 恒可用，mysql 走连接池探活，DB 挂了为 False）。
    插件 registry 属 M4，未构建仍返回 503 NOT_READY。
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
