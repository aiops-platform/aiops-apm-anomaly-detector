"""MySQL 连接池（aiomysql）。

两种用途：
- ``db=None``：裸库连接（迁移用，先 CREATE DATABASE 再 USE）；
- ``db=settings.db_name``：应用 store 用。

对外提供 ``acquire()`` 返回一个绑定连接的句柄（execute/fetchone/fetchall/commit），
以及便捷的 ``execute`` / ``fetchone`` / ``fetchall``（自动 acquire→commit→release）。
"""

from __future__ import annotations

from typing import Any

import aiomysql  # type: ignore[import-untyped]

from ..settings import Settings


class _ConnectionHandle:
    """包装单个 aiomysql 连接，暴露 store / runner 所需的窄接口。"""

    def __init__(self, conn: aiomysql.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, args: tuple = ()) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(sql, args)

    async def execute_lastid(self, sql: str, args: tuple = ()) -> int:
        """执行写入并返回 ``cursor.lastrowid``（M6 lease 原子取号用）。"""
        async with self._conn.cursor() as cur:
            await cur.execute(sql, args)
            return int(cur.lastrowid or 0)

    async def execute_affected(self, sql: str, args: tuple = ()) -> int:
        """执行写入并返回受影响行数 ``cursor.rowcount``（M6 lease 接管/续约用）。"""
        async with self._conn.cursor() as cur:
            await cur.execute(sql, args)
            return cur.rowcount

    async def fetchone(self, sql: str, args: tuple = ()) -> tuple | None:
        async with self._conn.cursor() as cur:
            await cur.execute(sql, args)
            return await cur.fetchone()

    async def fetchall(self, sql: str, args: tuple = ()) -> list[tuple]:
        async with self._conn.cursor() as cur:
            await cur.execute(sql, args)
            rows = await cur.fetchall()
            return list(rows)

    async def commit(self) -> None:
        await self._conn.commit()


class ConnectionPool:
    """aiomysql 连接池封装。"""

    def __init__(self, settings: Settings, *, db: str | None = None) -> None:
        self._settings = settings
        self._db = db if db is not None else settings.db_name
        self._pool: aiomysql.Pool | None = None

    async def init(self) -> None:
        if self._pool is not None:
            return
        self._pool = await aiomysql.create_pool(
            host=self._settings.db_host,
            port=self._settings.db_port,
            user=self._settings.db_user,
            password=self._settings.db_password,
            db=self._db,
            autocommit=False,
            connect_timeout=3,
            minsize=1,
            maxsize=5,
            charset="utf8mb4",
        )

    async def acquire(self) -> _ConnectionHandle:
        if self._pool is None:
            raise RuntimeError("connection pool not initialized")
        conn = await self._pool.acquire()
        return _ConnectionHandle(conn)

    async def release(self, handle: _ConnectionHandle) -> None:
        if self._pool is not None:
            await self._pool.release(handle._conn)

    async def execute(self, sql: str, args: tuple = ()) -> None:
        handle = await self.acquire()
        try:
            await handle.execute(sql, args)
            await handle.commit()
        finally:
            await self.release(handle)

    async def execute_lastid(self, sql: str, args: tuple = ()) -> int:
        """便捷：执行写入并返回 lastrowid（自动 acquire→commit→release）。"""
        handle = await self.acquire()
        try:
            lastid = await handle.execute_lastid(sql, args)
            await handle.commit()
            return lastid
        finally:
            await self.release(handle)

    async def execute_affected(self, sql: str, args: tuple = ()) -> int:
        """便捷：执行写入并返回受影响行数（自动 acquire→commit→release）。"""
        handle = await self.acquire()
        try:
            affected = await handle.execute_affected(sql, args)
            await handle.commit()
            return affected
        finally:
            await self.release(handle)

    async def fetchone(self, sql: str, args: tuple = ()) -> tuple | None:
        handle = await self.acquire()
        try:
            row = await handle.fetchone(sql, args)
            await handle.commit()
            return row
        finally:
            await self.release(handle)

    async def fetchall(self, sql: str, args: tuple = ()) -> list[tuple]:
        handle = await self.acquire()
        try:
            rows = await handle.fetchall(sql, args)
            await handle.commit()
            return rows
        finally:
            await self.release(handle)

    async def health_check(self) -> bool:
        try:
            row = await self.fetchone("SELECT 1")
            return row is not None and row[0] == 1
        except Exception:
            return False

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    @property
    def db(self) -> str | None:
        return self._db


def _as_json(value: Any) -> str:
    """把 Python 值转成 MySQL JSON 列可接收的 JSON 字符串。"""
    import json

    return json.dumps(value, ensure_ascii=False)


def _decode_json(value: Any) -> Any:
    """把 MySQL JSON 列值还原为 Python 对象（已是 dict/list 则原样返回）。"""
    import json

    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)
