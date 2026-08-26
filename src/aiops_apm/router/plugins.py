"""UC-4.1 / UC-4.2：/v1/plugins 插件列表与重载。

registry 由 M4 在 lifespan 加载进 ``app.state.registry``；``POST /reload`` 重新扫
entry_points 后原子替换快照（admin 权限校验留 M7 安全加固）。
"""

import asyncio

from fastapi import APIRouter, Request

from aiops_apm.exceptions import AppException, ErrorCode

router = APIRouter(prefix="/v1/plugins", tags=["plugins"])


def _registry(request: Request):
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise AppException(ErrorCode.INTERNAL, "plugin registry not loaded")
    return registry


@router.get("")
async def list_plugins(request: Request) -> dict:
    """返回三组已加载插件名（collector / detector / suppressor）。"""
    return _registry(request).list()


@router.post("/reload")
async def reload_plugins(request: Request) -> dict:
    """重新发现插件并原子替换快照，返回更新后的列表。"""
    state = request.app.state
    reg = _registry(request)
    # 第三方插件 build()/模块导入可能阻塞（设计 §5.4）→ 放线程执行，避免阻塞事件循环；
    # reg.reload 内部一次 `_active` 属性赋值原子替换快照（GIL 下指针交换安全）。
    await asyncio.to_thread(
        reg.reload,
        http=state.http_client,
        pool=getattr(state.storage, "pool", None),
        settings=state.settings,
    )
    return reg.list()
