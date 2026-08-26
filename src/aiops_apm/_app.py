"""FastAPI 应用工厂 + lifespan + 统一异常处理。

M0 为工程基座：进程能起、配置能加载、探针可用、异常标准化。
M6 起：lifespan 启动 scheduler + reconciler 后台任务；``settings.api_keys`` 非空时挂 AuthMiddleware。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth.middleware import AuthMiddleware
from .collectors import SharedHttpClient
from .exceptions import AppException, ErrorCode
from .plugins.registry import PluginRegistry
from .reconcile import Reconciler
from .router.api import api_router
from .scheduler import Scheduler
from .settings import Settings
from .storage import build_storage


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期钩子：M2 接线 storage；M3 接线共享出站 HTTP 客户端；M4 加载插件 registry；
    M6 启动 scheduler/reconciler 后台任务。

    **fail-fast**（用户确认）：mysql backend 连不上 DB 时 ``build_storage`` 抛异常，
    lifespan 启动失败 → uvicorn 进程退出。memory backend（demo/单测）无此约束。
    插件 registry 从 entry_points 发现并原子快照加载（单插件失败只告警不拖垮）。
    多副本下 scheduler 靠 lease 门控，仅一个副本实际调度（UC-6.9）。
    """
    settings: Settings = app.state.settings
    app.state.storage = await build_storage(settings)
    app.state.http_client = SharedHttpClient(settings)
    app.state.registry = PluginRegistry().load(
        http=app.state.http_client,
        pool=getattr(app.state.storage, "pool", None),
        settings=settings,
    )

    background_tasks: list = []
    if settings.enable_scheduler:
        scheduler = Scheduler(settings, app.state.registry, app.state.storage, http=app.state.http_client)
        app.state.scheduler = scheduler
        background_tasks.append(asyncio.create_task(scheduler.run()))
        reconciler = Reconciler(settings, app.state.storage)
        app.state.reconciler = reconciler
        background_tasks.append(asyncio.create_task(reconciler.run()))

    try:
        yield
    finally:
        if background_tasks:
            for task in background_tasks:
                task.cancel()
            for task in background_tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        await app.state.http_client.aclose()
        await app.state.storage.close()


def _status_for_code(code: ErrorCode) -> int:
    """ErrorCode -> HTTP 状态码。"""
    mapping = {
        ErrorCode.NOT_FOUND: 404,
        ErrorCode.PLUGIN_NOT_FOUND: 404,
        ErrorCode.VALIDATION: 400,
        ErrorCode.PERMISSION: 403,
    }
    return mapping.get(code, 500)


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建 FastAPI 应用实例。``settings`` 缺省时读取环境变量生成。

    ``settings.api_keys`` 非空才挂 AuthMiddleware（配置了才强制，UC-6.8）；
    未配置 = 放行（匿名 admin，既有 API 测试零改动）。
    """
    app = FastAPI(title="APM Alert Module", lifespan=lifespan)
    app.state.settings = settings if settings is not None else Settings()

    if app.state.settings.api_keys:
        app.add_middleware(AuthMiddleware, api_keys=app.state.settings.api_keys)

    app.include_router(api_router)

    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        return JSONResponse(
            status_code=_status_for_code(exc.code),
            content={
                "code": exc.code.value,
                "reason": exc.reason,
                "trace_id": exc.trace_id,
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            400: ErrorCode.VALIDATION,
            403: ErrorCode.PERMISSION,
            404: ErrorCode.NOT_FOUND,
        }.get(exc.status_code, ErrorCode.INTERNAL)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": code.value,
                "reason": exc.detail,
                "trace_id": uuid.uuid4().hex,
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "code": ErrorCode.INTERNAL.value,
                "reason": str(exc),
                "trace_id": uuid.uuid4().hex,
            },
        )

    return app
