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

    M0 为空壳，尚未构建存储/插件，因此恒返回 503 NOT_READY（满足 UC-0.1）。
    后续里程碑在 lifespan 中构建 app.state.storage / app.state.registry 后自动生效。
    """
    state = request.app.state
    checks = {
        "db": bool(getattr(state, "storage", None)),
        "plugins": bool(getattr(state, "registry", None)),
    }
    if not all(checks.values()):
        return JSONResponse(status_code=503, content={"code": "NOT_READY", "reason": str(checks)})
    return JSONResponse(status_code=200, content={"status": "ready", "checks": checks})
