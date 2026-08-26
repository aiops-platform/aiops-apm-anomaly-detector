"""M2 存储聚合：build_storage(memory) 分派 + 健康检查。"""

from aiops_apm.settings import Settings
from aiops_apm.storage import Storage, build_storage


async def test_build_storage_memory() -> None:
    settings = Settings(_env_file=None, storage_backend="memory")
    storage = await build_storage(settings)
    assert isinstance(storage, Storage)
    assert storage.records is not None
    assert storage.domain_configs is not None
    assert storage.pool is None  # memory 无连接池
    assert await storage.health_check() is True
    await storage.close()  # 无副作用


async def test_storage_rejects_invalid_backend() -> None:
    settings = Settings(_env_file=None, storage_backend="sqlite")
    try:
        await build_storage(settings)
    except ValueError as exc:
        assert "storage_backend" in str(exc)
    else:
        raise AssertionError("未知 backend 应抛 ValueError")
