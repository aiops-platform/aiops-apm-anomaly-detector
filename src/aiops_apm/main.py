"""应用入口：``uvicorn aiops_apm.main:app`` 暴露 FastAPI 应用（含 lifespan）。

M7 起单独抽出本模块，供 Docker CMD / uvicorn 直接引用 ``app``（含 lifespan 启动
scheduler/reconciler 与 fail-fast storage 接线）。
"""

from aiops_apm._app import create_app

app = create_app()

__all__ = ["app"]
