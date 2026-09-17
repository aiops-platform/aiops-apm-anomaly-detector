"""PostgreSQL 连接池（psycopg3）。

单实例、单库单 schema：所有表建在 ``settings.db_name`` 库内的 ``settings.db_schema``
schema 下，与同库其它服务（如 agentflow 自己在 ``public`` 里的表）隔离。

两个必须在**连接串**里钉死的会话参数（都走 pool 的 ``kwargs.options``，不能 ``open()``
之后再 ``SET``——psycopg_pool 懒创建连接，后建的连接会拿不到）：

- ``search_path``：PG 没有 MySQL 的 ``USE``，表定位靠会话 search_path。若漏设，症状是部分
  请求报 ``42P01``，或**静默命中 ``public`` 里的同名表**。
- ``TimeZone=UTC``：见 ``_to_naive_utc`` 的说明。

对外提供 ``acquire()`` 返回一个绑定连接的句柄（execute/fetchone/fetchall/commit），
以及便捷的 ``execute`` / ``fetchone`` / ``fetchall``（自动 acquire→commit→release）。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from ..settings import Settings

# schema 就绪探针看的表：取一个迁移 V1 必建、且几乎所有路径都会用到的核心表。
_READY_PROBE_TABLE = "problem_record"


def _json_default(o: Any) -> Any:
    """``json.dumps`` 的 default 钩子：datetime/date 转 isoformat。

    emit 的 ``metric_anomalies.detected_at`` 等是 ``datetime``，json.dumps 默认序列化不了
    → TypeError。
    """
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _to_naive_utc(value: Any) -> Any:
    """把 aware datetime 归一为 naive UTC。

    列类型是 ``TIMESTAMP(3)``（无时区），库内约定存 **naive UTC**（见 ``snapshots.py``
    的 ``_now_naive_utc`` 与 ``collectors/_window.py`` 的水位线注释）。

    psycopg3 按 ``obj.tzinfo`` 选 dumper：aware → ``timestamptz``（写 naive 列时会被 PG
    按**会话时区**折算），naive → ``timestamp``（原样写入）。若不做归一，写入结果就依赖
    会话时区——目标 PG 容器设了 ``TZ: Asia/Shanghai``，会让同一张表里混进相差 8 小时的
    两种时间，且不报错。这里先把 aware 转成 naive UTC，让写入与会话时区无关。
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _bind(args: tuple) -> tuple:
    return tuple(_to_naive_utc(a) for a in args)


class _ConnectionHandle:
    """包装单个 psycopg 连接，暴露 store / runner 所需的窄接口。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def _run(self, cur: Any, sql: str, args: tuple) -> None:
        """统一的执行入口。

        注意这个分支不是微优化：psycopg3 在 ``params`` 非 None 时会扫描整条 SQL 找 ``%``
        占位符，遇到裸 ``%`` 直接抛 ``ProgrammingError``——而且**不认字符串字面量里的 %**。
        本仓库的 DDL 含大量 ``COMMENT ON ... IS '...'``，store 层 SQL 也并非全无 ``%``。
        aiomysql 只在 args 为真值时才格式化，所以传 ``()`` 一直无害；这是 PG 引入的新
        失败模式，靠"传 None 而非 ()"规避。
        """
        if args:
            await cur.execute(sql, _bind(args))
        else:
            await cur.execute(sql)

    async def execute(self, sql: str, args: tuple = ()) -> None:
        async with self._conn.cursor() as cur:
            await self._run(cur, sql, args)

    async def execute_returning(self, sql: str, args: tuple = ()) -> Any:
        """执行写入并返回 ``RETURNING`` 的第一列。

        取代 MySQL 的 ``cursor.lastrowid``（psycopg3 无此属性）。比 MySQL 的
        "写 + 另发 SELECT LAST_INSERT_ID()" 少一次往返，且不依赖连接作用域。
        """
        async with self._conn.cursor() as cur:
            await self._run(cur, sql, args)
            row = await cur.fetchone()
            return None if row is None else row[0]

    async def execute_affected(self, sql: str, args: tuple = ()) -> int:
        """执行写入并返回受影响行数（M6 lease 接管/续约、records CAS 用）。

        语义差异提醒：MySQL 的 ``rowcount`` 数**实际变更**的行，PG 数**匹配**的行。
        现有三个 ``affected == 1`` 断言处写入值必然变化，两者等价。
        """
        async with self._conn.cursor() as cur:
            await self._run(cur, sql, args)
            return cur.rowcount

    async def fetchone(self, sql: str, args: tuple = ()) -> tuple | None:
        async with self._conn.cursor() as cur:
            await self._run(cur, sql, args)
            return await cur.fetchone()

    async def fetchall(self, sql: str, args: tuple = ()) -> list[tuple]:
        async with self._conn.cursor() as cur:
            await self._run(cur, sql, args)
            rows = await cur.fetchall()
            return list(rows)

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        await self._conn.rollback()


class ConnectionPool:
    """psycopg3 异步连接池封装。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: AsyncConnectionPool | None = None

    @property
    def conninfo(self) -> str:
        """连接串（不含 session options——那些走 kwargs，见 ``session_options``）。"""
        return make_conninfo(
            host=self._settings.db_host,
            port=self._settings.db_port,
            user=self._settings.db_user,
            password=self._settings.db_password,
            dbname=self._settings.db_name,
            connect_timeout=3,
        )

    @property
    def session_options(self) -> str:
        """每连接都要生效的会话参数。

        走 pool 的 ``kwargs`` 而不是拼进 conninfo：两者的 ``options`` 会互相覆盖（kwargs
        胜），只留一处避免"改了没生效"。
        """
        return f"-c search_path={self._settings.db_schema} -c TimeZone=UTC"

    async def init(self) -> None:
        if self._pool is not None:
            return
        # open=False + 显式 await open()：避免在事件循环外构造（psycopg_pool 会告警）。
        self._pool = AsyncConnectionPool(
            conninfo=self.conninfo,
            kwargs={"options": self.session_options},
            min_size=1,
            max_size=5,
            open=False,
        )
        await self._pool.open()
        # 建 schema 是迁移的职责；这里只确认连得上，连不上就让 build_storage 抛出去
        # （_app.py 的 fail-fast 语义）。

    async def acquire(self) -> _ConnectionHandle:
        if self._pool is None:
            raise RuntimeError("connection pool not initialized")
        conn = await self._pool.getconn()
        return _ConnectionHandle(conn)

    async def release(self, handle: _ConnectionHandle) -> None:
        if self._pool is None:
            return
        # 显式 rollback：PG 的事务一旦出错就进入 aborted 态，后续语句全部报 25P02。
        # poller/_app 会吞掉单轮异常并继续复用连接池，不清理就会级联失败——这是 MySQL
        # 时代不存在的失败模式。成功路径上 commit 已经发生，这里的 rollback 是空操作。
        try:
            await handle.rollback()
        except Exception:
            pass
        await self._pool.putconn(handle._conn)

    async def execute(self, sql: str, args: tuple = ()) -> None:
        handle = await self.acquire()
        try:
            await handle.execute(sql, args)
            await handle.commit()
        finally:
            await self.release(handle)

    async def execute_returning(self, sql: str, args: tuple = ()) -> Any:
        """便捷：执行写入并返回 RETURNING 首列（自动 acquire→commit→release）。"""
        handle = await self.acquire()
        try:
            result = await handle.execute_returning(sql, args)
            await handle.commit()
            return result
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

    async def schema_ready(self) -> bool:
        """``search_path`` 上能否看到核心表 —— 即迁移是否已跑过。

        为什么不能只探 ``SELECT 1``：PG 下库是共享的（默认就是 multi-agent-workflow 的
        ``agentflow``），**schema 缺失时连接照样成功**。只探连通性会让服务在没跑迁移时
        正常启动、``/ready`` 报 ready，而每个真实查询都 500
        （``relation "monitor_target" does not exist``）——k8s 下会把流量打到坏 Pod 上。
        MySQL 时代库不存在时连接本身就失败，所以没有这个问题，这是 PG 化引入的回归。

        用 ``to_regclass`` 而非查 ``pg_tables``：它走的是当前 ``search_path`` 的解析结果，
        正是查询实际会用到的可见性。返回 None 表示该表在 search_path 上看不见。
        """
        try:
            row = await self.fetchone(f"SELECT to_regclass('{_READY_PROBE_TABLE}') IS NOT NULL")
            return row is not None and bool(row[0])
        except Exception:
            return False

    async def health_check(self) -> bool:
        return await self.schema_ready()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


def _as_json(value: Any) -> Jsonb:
    """把 Python 值包成 psycopg3 可绑定的 JSONB 参数。

    必须包 ``Jsonb`` 而不能直接传 dict：psycopg3 没有 dict 的 dumper，会报
    "cannot adapt type 'dict'"。（传 str 倒是能work——走 unknown OID——但那样
    ``jsonb_build_array(%s)`` 之类会退化成 JSON 字符串元素，见 records.append_evidence。）

    ``dumps`` 是本项目特有的：JSON 列里有 datetime（emit 的 metric_anomalies.detected_at），
    而 ``Jsonb`` 默认的 json.dumps 没有 default 钩子，且**不在构造时序列化**——不传
    ``dumps`` 会把一个原本在调用点立刻抛的 TypeError 推迟到 cur.execute 里才炸。
    """
    return Jsonb(value, dumps=_dumps)


def _decode_json(value: Any) -> Any:
    """jsonb 列值还原为 Python 对象。

    psycopg3 的 jsonb loader 已自动解析为 dict/list/int/str/bool/None，这里**必须**
    原样透传——补一个 ``json.loads`` 会在 jsonb 标量上炸（``'"x"'::jsonb`` 得到 str
    ``'x'``，``json.loads('x')`` → JSONDecodeError）。
    """
    return value
