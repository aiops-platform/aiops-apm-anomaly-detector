"""``SequenceStore`` 取号：``record_seq`` 出 PR-YYYYMMDD-NNNN、``ticket_seq`` 出 INC-YYYYMMDD-NNNN。

覆盖：格式；同日期递增；跨日期归 1；``%04d`` 补零；两串号计数器互不影响。
"""

from datetime import datetime, timezone

from aiops_apm.storage.sequence import InMemorySequenceStore


def _at(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def _store(holder: dict) -> InMemorySequenceStore:
    return InMemorySequenceStore(now=lambda: holder["now"])


async def test_next_id_format() -> None:
    holder = {"now": _at(2026, 8, 26)}
    rid = await _store(holder).next_id("application")
    assert rid == "PR-20260826-0001"


async def test_next_id_increments_same_day() -> None:
    holder = {"now": _at(2026, 8, 26)}
    s = _store(holder)
    assert await s.next_id("application") == "PR-20260826-0001"
    assert await s.next_id("application") == "PR-20260826-0002"
    assert await s.next_id("application") == "PR-20260826-0003"


async def test_next_id_resets_on_new_day() -> None:
    holder = {"now": _at(2026, 8, 26)}
    s = _store(holder)
    await s.next_id("application")
    await s.next_id("application")
    holder["now"] = _at(2026, 8, 27)
    assert await s.next_id("application") == "PR-20260827-0001"


async def test_next_id_zero_padding() -> None:
    holder = {"now": _at(2026, 8, 26)}
    s = _store(holder)
    for _ in range(9):
        await s.next_id("application")
    assert await s.next_id("application") == "PR-20260826-0010"


# ── 工单号（「升级」派单用）：INC-YYYYMMDD-NNNN，**与 PR 分开计数** ────────────

async def test_next_ticket_number_format() -> None:
    holder = {"now": _at(2026, 9, 21)}
    assert await _store(holder).next_ticket_number() == "INC-20260921-0001"


async def test_ticket_series_is_separate_from_record_series() -> None:
    """两串号共用同一套格式与取号机制，但**计数器分开**。

    共用一张 ``record_seq`` 的话（它的 PK 是 ``seq_date``）两种号会互相跳号：
    ``PR-…-0007`` 与 ``INC-…-0007`` 并存、两边都有空洞，排查时像丢号。
    """
    holder = {"now": _at(2026, 9, 21)}
    s = _store(holder)
    assert await s.next_id("application") == "PR-20260921-0001"
    assert await s.next_ticket_number() == "INC-20260921-0001"  # 不跟着 PR 走成 0002
    assert await s.next_id("application") == "PR-20260921-0002"
    assert await s.next_ticket_number() == "INC-20260921-0002"


async def test_ticket_number_resets_on_new_day() -> None:
    holder = {"now": _at(2026, 9, 21)}
    s = _store(holder)
    await s.next_ticket_number()
    holder["now"] = _at(2026, 9, 22)
    assert await s.next_ticket_number() == "INC-20260922-0001"
