"""FastAPI 应用工厂 + lifespan + 统一异常处理。

M0 为工程基座：进程能起、配置能加载、探针可用、异常标准化。
lifespan 中的连接池 / 插件注册 / 调度器在后续里程碑填充。
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .exceptions import AppException, ErrorCode
from .router.api import api_router
from .settings import Settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期钩子。

    TODO(M2): 初始化 MySQL 连接池 / InMemory 存储
    TODO(M4): 加载插件 registry
    TODO(M6): 启动 scheduler 后台任务
    """
    yield
    # shutdown 阶段：关闭连接池、停止 scheduler（后续里程碑填充）


def _status_for_code(code: ErrorCode) -> int:
    """ErrorCode -> HTTP 状态码。"""
    mapping = {
        ErrorCode.NOT_FOUND: 404,
        ErrorCode.VALIDATION: 400,
        ErrorCode.PERMISSION: 403,
    }
    return mapping.get(code, 500)


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建 FastAPI 应用实例。``settings`` 缺省时读取环境变量生成。"""
    app = FastAPI(title="APM Alert Module", lifespan=lifespan)
    app.state.settings = settings if settings is not None else Settings()

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
