"""取号：``record_seq`` / ``ticket_seq`` 两张表各自按日期原子取号。

- ``SequenceStore``（ABC）：``next_id`` = M5 emit 生成 ``record_id``（``PR-YYYYMMDD-NNNN``）；
  ``next_ticket_number`` = 「升级」时给派出去的修复工单取号（``INC-YYYYMMDD-NNNN``）。
- ``InMemorySequenceStore``：单测/demo 真源（``now`` 可注入以测跨日期）。
- ``PGSequenceStore``：``INSERT ... ON CONFLICT DO UPDATE ... RETURNING next_seq`` 原子取号。

**两串号共用同一套格式与取号机制，但计数器分开**（两张表）。共用一张表的话——
``record_seq`` 的 PK 是 ``seq_date``——两种号会互相跳号（``PR-…-0007`` 与 ``INC-…-0007``
并存，且两边都有空洞），排查时像丢号。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timezone

from .connection import ConnectionPool

#: 号段前缀（与 ``record_seq`` / ``ticket_seq`` 两张表一一对应）
RECORD_PREFIX = "PR"
TICKET_PREFIX = "INC"


class SequenceStore(ABC):
    """按日期分段的取号接口。"""

    @abstractmethod
    async def next_id(self, domain: str) -> str:
        """返回 ``PR-YYYYMMDD-NNNN``（NNNN=该日自增）。domain 参数保留（骨架签名），InMemory 忽略。"""

    @abstractmethod
    async def next_ticket_number(self) -> str:
        """返回 ``INC-YYYYMMDD-NNNN``（NNNN=该日自增，**与 PR 分开计数**）。

        给「升级」派出的修复工单用（APM ``POST /v1/problems/{id}/diagnose/decision`` 的
        ``escalate`` 分支）。选 INC 前缀是为了贴近 ITSM 的既有习惯；格式沿用本仓
        ``PR-`` 的日期分段约定，不是新发明。
        """


def _date_key(now: datetime) -> str:
    return now.strftime("%Y%m%d")


class InMemorySequenceStore(SequenceStore):
    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        # 键含号段前缀：两串号同一天各自从 0001 起，互不影响
        self._next: dict[tuple[str, str], int] = {}

    async def next_id(self, domain: str) -> str:
        seq_date = _date_key(self._now())
        return f"{RECORD_PREFIX}-{seq_date}-{self._bump(RECORD_PREFIX, seq_date):04d}"

    async def next_ticket_number(self) -> str:
        seq_date = _date_key(self._now())
        return f"{TICKET_PREFIX}-{seq_date}-{self._bump(TICKET_PREFIX, seq_date):04d}"

    def _bump(self, prefix: str, seq_date: str) -> int:
        """``seq_date`` 由调用方算好再传进来——**不在本方法里再取一次 now()**：
        跨零点时两次 ``now()`` 会落到不同的日期，号段前缀与计数器就对不上了。"""
        key = (prefix, seq_date)
        n = self._next.get(key, 0) + 1
        self._next[key] = n
        return n


class PGSequenceStore(SequenceStore):
    #: 表名是**模块常量**，不是外部输入——标识符没法参数化，只能 f-string 拼进 SQL
    _RECORD_SEQ = "record_seq"
    _TICKET_SEQ = "ticket_seq"

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def next_id(self, domain: str) -> str:
        seq_date = _date_key(datetime.now(timezone.utc))
        return f"{RECORD_PREFIX}-{seq_date}-{await self._bump(self._RECORD_SEQ, seq_date):04d}"

    async def next_ticket_number(self) -> str:
        seq_date = _date_key(datetime.now(timezone.utc))
        return f"{TICKET_PREFIX}-{seq_date}-{await self._bump(self._TICKET_SEQ, seq_date):04d}"

    async def _bump(self, table: str, seq_date: str) -> int:
        """``RETURNING`` 直接拿到自增后的值，少一次往返。

        顺带修掉 MySQL 版的隐患：``LAST_INSERT_ID()`` 在「当天首次插入」时返回的是
        **连接的上一个** ``LAST_INSERT_ID``（池化连接下非 0），会产出 ``…-0000`` 或重号；
        这里恒为 1。

        ``seq_date`` 由调用方算好再传进来——**不在本方法里再取一次 now()**：跨零点时
        两次 ``now()`` 会落到不同的日期，号段前缀与计数器就对不上了。
        """
        n = await self._pool.execute_returning(
            f"INSERT INTO {table} (seq_date, next_seq) VALUES (%s, 1) "
            f"ON CONFLICT (seq_date) DO UPDATE SET next_seq = {table}.next_seq + 1 "
            "RETURNING next_seq",
            (seq_date,),
        )
        return int(n or 1)
