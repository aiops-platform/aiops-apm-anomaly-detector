"""问题单取号：``record_seq`` 表 PR-YYYYMMDD-NNNN 原子取号。

- ``SequenceStore``（ABC）：M5 emit 生成 ``record_id`` 用。
- ``InMemorySequenceStore``：单测/demo 真源（``now`` 可注入以测跨日期）。
- ``MySQLSequenceStore``：``INSERT ... ON DUPLICATE KEY UPDATE next_seq=LAST_INSERT_ID(next_seq+1)``
  原子取号（best-effort；M5 以 InMemory 真源为准，真库验证待 DB 可用补跑）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timezone

from .connection import ConnectionPool


class SequenceStore(ABC):
    """``record_id`` 取号接口。"""

    @abstractmethod
    async def next_id(self, domain: str) -> str:
        """返回 ``PR-YYYYMMDD-NNNN``（NNNN=该日自增）。domain 参数保留（骨架签名），InMemory 忽略。"""


def _date_key(now: datetime) -> str:
    return now.strftime("%Y%m%d")


class InMemorySequenceStore(SequenceStore):
    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._next: dict[str, int] = {}

    async def next_id(self, domain: str) -> str:
        seq_date = _date_key(self._now())
        n = self._next.get(seq_date, 0) + 1
        self._next[seq_date] = n
        return f"PR-{seq_date}-{n:04d}"


class MySQLSequenceStore(SequenceStore):
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def next_id(self, domain: str) -> str:
        seq_date = _date_key(datetime.now(timezone.utc))
        handle = await self._pool.acquire()
        try:
            await handle.execute(
                "INSERT INTO record_seq (seq_date, next_seq) VALUES (%s, 1) "
                "ON DUPLICATE KEY UPDATE next_seq = LAST_INSERT_ID(next_seq + 1)",
                (seq_date,),
            )
            row = await handle.fetchone("SELECT LAST_INSERT_ID()")
            await handle.commit()
        finally:
            await self._pool.release(handle)
        n = int(row[0]) if row else 1
        return f"PR-{seq_date}-{n:04d}"
