"""FastAPI 应用工厂 + lifespan + 统一异常处理。

M0 为工程基座：进程能起、配置能加载、探针可用、异常标准化。
M6 起：lifespan 启动 scheduler + reconciler 后台任务；``settings.api_keys`` 非空时挂 AuthMiddleware。
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from .audit import set_audit_enabled
from .auth.middleware import AuthMiddleware
from .collectors import SharedHttpClient
from .collectors._gateway import OutboundGateway
from .exceptions import AppException, ErrorCode
from .plugins.registry import PluginRegistry
from .reconcile import Reconciler
from .router.api import api_router
from .scheduler import Scheduler
from .settings import Settings
from .storage import build_storage

logger = logging.getLogger(__name__)


async def _supervise(
    coro_factory: Callable[[], Awaitable[None]],
    name: str,
    *,
    delay: float = 1.0,
    max_delay: float = 60.0,
) -> None:
    """守护后台任务：worker 意外异常时记录并退避重启；停止时取消直接传播。

    与 ``_run_group`` 的单轮隔离互补：那层保证单轮失败不 kill 调度循环，
    这层保证即使 worker 循环自身崩溃（lease/找目标等阶段的异常）也能复活，
    不会出现「应用还活着、但调度静默停止」的状态。
    """
    while True:
        try:
            await coro_factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- 守护重启
            logger.error(
                "background task %s crashed: %s: %s; restart in %.1fs",
                name, type(exc).__name__, exc, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


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
    set_audit_enabled(settings.audit_enabled)
    OutboundGateway.set_allow_loopback(settings.allow_loopback)  # 本地联调放行回环（默认关）
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
        background_tasks.append(asyncio.create_task(_supervise(scheduler.run, "scheduler")))
        if settings.enable_reconciler:
            # enable_reconciler=false 时只关自动关单（reconcile），检测轮次照常跑。
            reconciler = Reconciler(settings, app.state.storage)
            app.state.reconciler = reconciler
            background_tasks.append(asyncio.create_task(_supervise(reconciler.run, "reconciler")))

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
        ErrorCode.CONFIG_ERROR: 400,
        ErrorCode.PERMISSION: 403,
        ErrorCode.CONFLICT: 409,
        ErrorCode.UPSTREAM: 502,
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

    if app.state.settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=app.state.settings.allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["X-Tenant-Id", "Authorization", "Content-Type"],
        )

    app.include_router(api_router)

    @app.get("/metrics", include_in_schema=False)
    async def _metrics() -> Response:
        """Prometheus 指标（UC-7.1）：`generate_latest()` 暴露默认注册表全部指标。"""
        return Response(generate_latest(), media_type="text/plain; version=0.0.4; charset=utf-8")

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
