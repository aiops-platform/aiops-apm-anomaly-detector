"""迁移执行器（`make migrate` 入口）。

按版本顺序幂等执行 ``migrations/V<version>__*.sql``：

1. 钉住单个连接（``acquire``）——``USE`` 只对该连接生效，必须全程同一连接。
2. 建 ``schema_versions`` 追踪表，读当前版本。
3. 逐脚本执行 > current 的语句，成功后记录版本号。

``pool`` 用 ``db=None`` 的裸库连接（先 ``CREATE DATABASE`` 再 ``USE``），
应用 store 的连接池用 ``db=settings.db_name``（见 ``storage/connection.py``）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..settings import Settings


@dataclass
class MigrationScript:
    """单个迁移脚本：版本号 + 原始 SQL。"""

    version: int
    path: Path
    sql: str


class MigrationRunner:
    """幂等迁移执行器。``pool`` 需提供 ``acquire()`` / ``release(conn)``。"""

    def __init__(self, pool: object, schema: str, scripts_dir: Path | None = None) -> None:
        self._pool = pool
        self._schema = schema
        self._scripts_dir = scripts_dir if scripts_dir is not None else Path(__file__).parent

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
        """按 ``;`` 拆分语句，忽略 ``--`` 行注释与引号内的分号。"""
        statements: list[str] = []
        buf: list[str] = []
        in_single = in_double = in_backtick = False
        i = 0
        n = len(sql)
        while i < n:
            ch = sql[i]
            if not (in_single or in_double or in_backtick) and sql.startswith("--", i):
                while i < n and sql[i] != "\n":
                    i += 1
                continue
            if ch == "'" and not (in_double or in_backtick):
                in_single = not in_single
            elif ch == '"' and not (in_single or in_backtick):
                in_double = not in_double
            elif ch == "`" and not (in_single or in_double):
                in_backtick = not in_backtick
            elif ch == ";" and not (in_single or in_double or in_backtick):
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
            await handle.execute(
                f"CREATE DATABASE IF NOT EXISTS {self._schema} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            await handle.execute(f"USE {self._schema}")
            await handle.execute(
                "CREATE TABLE IF NOT EXISTS schema_versions ("
                "version INT NOT NULL PRIMARY KEY,"
                "applied_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)"
                ") ENGINE=InnoDB"
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
    """连裸库跑迁移，返回应用数量。"""
    from ..storage.connection import ConnectionPool

    pool = ConnectionPool(settings, db=None)
    await pool.init()
    runner = MigrationRunner(pool, schema=settings.db_name)
    try:
        return await runner.migrate()
    finally:
        await pool.close()


def main() -> None:
    """``python -m aiops_apm.migrations.runner``（Makefile `migrate` 调用）。"""
    applied = asyncio.run(run_migrations(Settings()))
    print(f"[migrate] applied {applied} script(s); schema_versions -> aiops_apm_runtime")


if __name__ == "__main__":
    main()
