"""M2 存储聚合：build_storage(memory) 分派 + 健康检查 + 连接池 db 语义。"""

from aiops_apm.settings import Settings
from aiops_apm.storage import Storage, build_storage
from aiops_apm.storage.connection import ConnectionPool


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


async def test_storage_rejects_legacy_mysql_backend() -> None:
    """M8 起 ``mysql`` 不再是合法 backend。

    这条是防回归的：若 ``build_storage`` 里残留 ``if backend == "mysql":`` 分支，
    只断言 "sqlite" 被拒是发现不了的（那条分支不会被执行到）。
    """
    settings = Settings(_env_file=None, storage_backend="mysql")
    try:
        await build_storage(settings)
    except ValueError as exc:
        assert "storage_backend" in str(exc)
    else:
        raise AssertionError("mysql backend 已下线，应抛 ValueError")


def test_pool_conninfo_and_session_options() -> None:
    """连接串与每连接会话参数的构造。

    两件事必须同时成立，否则会出现"部分连接查不到表"或"时间戳差 8 小时"这类只在
    边界条件下暴露的问题：
      - ``search_path`` / ``TimeZone`` 走 pool 的 kwargs（每连接都生效），不拼进 conninfo
        （两处都写会互相覆盖，且 psycopg_pool 懒建连接时 open() 后 SET 摸不到新连接）；
      - conninfo 本身指向 settings.db_name 库。
    """
    settings = Settings(_env_file=None, db_name="agentflow", db_schema="aiops_apm_runtime")
    pool = ConnectionPool(settings)
    assert "dbname=agentflow" in pool.conninfo
    assert "search_path" not in pool.conninfo  # 会话参数不在这里
    assert "search_path=aiops_apm_runtime" in pool.session_options
    assert "TimeZone=UTC" in pool.session_options
