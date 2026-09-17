"""迁移执行器（`make migrate` 入口）。

按版本顺序幂等执行 ``migrations/V<version>__*.sql``：

1. 钉住单个连接（``acquire``）——``SET search_path`` 只对该连接生效，必须全程同一连接。
2. 建 ``aiops_apm_runtime`` schema 与 ``schema_versions`` 追踪表，读当前版本。
3. 逐脚本执行 > current 的语句，成功后记录版本号。

M8（PG 化）：连接池直连 ``settings.db_name`` 库（库必须已存在），表建在 ``settings.db_schema``
schema 里。PG 没有 MySQL 的 ``USE``，也没有"连不上库就先建库"的鸡生蛋问题——原先为绕开它
而引入的 ``db=None`` 裸库连接模式已随之删除。

注意 PG 的 DDL 是**事务性**的：MySQL 下 DDL 隐式提交，中途失败会留下半截 schema；这里整轮
是一个事务，任一条语句失败会回滚全部（含 ``schema_versions`` 记录）。这是刻意的改进，但
也意味着单条 DDL 写错会让整个迁移挂掉而不是挂一半。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from ..settings import Settings

# 美元引用定界符：$$ 或 $tag$（PG 的 PL/pgSQL 函数体、DO 块用）。
# $1 这类位置参数不匹配（没有配对的收尾 $）。
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


@dataclass
class MigrationScript:
    """单个迁移脚本：版本号 + 原始 SQL。"""

    version: int
    path: Path
    sql: str


class MigrationRunner:
    """幂等迁移执行器。``pool`` 需提供 ``acquire()`` / ``release(conn)``。"""

    def __init__(
        self,
        pool: object,
        schema: str,
        scripts_dir: Path | None = None,
        *,
        testbed_es_url: str = "http://localhost:19200/app-logs/_search",
    ) -> None:
        self._pool = pool
        self._schema = schema
        self._scripts_dir = scripts_dir if scripts_dir is not None else Path(__file__).parent
        self._testbed_es_url = testbed_es_url

    # ---- 纯函数（可单测）----

    def _load_scripts(self) -> list[MigrationScript]:
        """加载 ``V<num>__<name>.sql``，按版本号升序。"""
        scripts: list[MigrationScript] = []
        for path in self._scripts_dir.glob("V*__*.sql"):
            stem = path.stem  # V1__init_tables
            version = int(stem.split("__", 1)[0][1:])
            scripts.append(MigrationScript(version=version, path=path, sql=path.read_text(encoding="utf-8")))
        return sorted(scripts, key=lambda s: s.version)

    def _split_statements(self, sql: str) -> list[str]:
        """按 ``;`` 拆分语句，忽略 ``--`` 行注释、普通引号内与**美元引用体内**的分号。

        美元引用（``$$ ... $$`` / ``$tag$ ... $tag$``）是 PG 特有的：PL/pgSQL 函数体里
        合法地含 ``;``、``'``、``"``，甚至形如 ``-- don't`` 的注释。因此进入美元引用后
        必须**整段原样吞掉、不参与任何引号/注释状态翻转**——只给注释检查加一个
        ``and not in_dollar`` 而保留引号翻转是错的：函数体里的一个撇号就会翻转
        ``in_single``，把后续所有语句截断成一条。
        """
        statements: list[str] = []
        buf: list[str] = []
        in_single = in_double = False
        in_dollar: str | None = None
        i = 0
        n = len(sql)
        while i < n:
            ch = sql[i]

            if in_dollar is not None:
                if sql.startswith(in_dollar, i):
                    buf.append(in_dollar)
                    i += len(in_dollar)
                    in_dollar = None
                    continue
                buf.append(ch)
                i += 1
                continue

            if not (in_single or in_double) and sql.startswith("--", i):
                while i < n and sql[i] != "\n":
                    i += 1
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "$" and not (in_single or in_double):
                match = _DOLLAR_TAG.match(sql, i)
                if match is not None:
                    in_dollar = match.group(0)
                    buf.append(in_dollar)
                    i = match.end()
                    continue
            elif ch == ";" and not (in_single or in_double):
                stmt = "".join(buf).strip()
                if stmt:
                    statements.append(stmt)
                buf = []
                i += 1
                continue
            buf.append(ch)
            i += 1
        tail = "".join(buf).strip()
        if tail:
            statements.append(tail)
        return statements

    # ---- 异步执行 ----

    async def migrate(self) -> int:
        """执行所有 > 当前版本的脚本，返回本次应用的数量。"""
        handle = await self._pool.acquire()  # type: ignore[attr-defined]
        try:
            await handle.execute(f"CREATE SCHEMA IF NOT EXISTS {self._schema}")
            await handle.execute(f"SET search_path TO {self._schema}")
            # 以 GUC 把环境相关的配置注入给迁移脚本 —— SQL 是静态文件读不到环境变量，
            # 而 V9 要往 source_config 里写 ES 地址（本机 port-forward 是 localhost:19200，
            # 容器里得是 host.containers.internal:19200）。脚本侧用
            # current_setting('aiops.testbed_es_url', true) 取，取不到再 COALESCE 兜底。
            # 用 set_config 带参数而非拼 SET 语句：值来自环境变量，拼接会有转义问题。
            await handle.execute(
                "SELECT set_config('aiops.testbed_es_url', %s, false)", (self._testbed_es_url,)
            )
            await handle.execute(
                "CREATE TABLE IF NOT EXISTS schema_versions ("
                "version INT NOT NULL PRIMARY KEY,"
                "applied_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)"
                ")"
            )
            row = await handle.fetchone("SELECT COALESCE(MAX(version), 0) FROM schema_versions")
            current = int(row[0]) if row is not None else 0

            applied = 0
            for script in self._load_scripts():
                if script.version <= current:
                    continue
                for stmt in self._split_statements(script.sql):
                    if stmt:
                        await handle.execute(stmt)
                await handle.execute("INSERT INTO schema_versions (version) VALUES (%s)", (script.version,))
                applied += 1
            await handle.commit()
            return applied
        finally:
            await self._pool.release(handle)  # type: ignore[attr-defined]


async def run_migrations(settings: Settings) -> int:
    """连库跑迁移，返回应用数量。"""
    from ..storage.connection import ConnectionPool

    pool = ConnectionPool(settings)
    await pool.init()
    runner = MigrationRunner(pool, schema=settings.db_schema, testbed_es_url=settings.testbed_es_url)
    try:
        return await runner.migrate()
    finally:
        await pool.close()


def main() -> None:
    """``python -m aiops_apm.migrations.runner``（Makefile `migrate` 调用）。"""
    settings = Settings()
    applied = asyncio.run(run_migrations(settings))
    print(f"[migrate] applied {applied} script(s); schema -> {settings.db_name}.{settings.db_schema}")


if __name__ == "__main__":
    main()
