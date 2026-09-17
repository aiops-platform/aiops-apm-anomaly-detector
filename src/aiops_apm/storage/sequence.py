"""问题单取号：``record_seq`` 表 PR-YYYYMMDD-NNNN 原子取号。

- ``SequenceStore``（ABC）：M5 emit 生成 ``record_id`` 用。
- ``InMemorySequenceStore``：单测/demo 真源（``now`` 可注入以测跨日期）。
- ``PGSequenceStore``：``INSERT ... ON CONFLICT DO UPDATE ... RETURNING next_seq`` 原子取号。
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


class PGSequenceStore(SequenceStore):
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def next_id(self, domain: str) -> str:
        seq_date = _date_key(datetime.now(timezone.utc))
        # RETURNING 直接拿到自增后的值，少一次往返。顺带修掉 MySQL 版的隐患：
        # LAST_INSERT_ID() 在「当天首次插入」时返回的是**连接的上一个** LAST_INSERT_ID
        # （池化连接下非 0），会产出 PR-YYYYMMDD-0000 或重号；这里恒为 1。
        n = await self._pool.execute_returning(
            "INSERT INTO record_seq (seq_date, next_seq) VALUES (%s, 1) "
            "ON CONFLICT (seq_date) DO UPDATE SET next_seq = record_seq.next_seq + 1 "
            "RETURNING next_seq",
            (seq_date,),
        )
        return f"PR-{seq_date}-{int(n or 1):04d}"
