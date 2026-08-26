"""``SequenceStore``：record_seq 取号 PR-YYYYMMDD-NNNN。

覆盖：格式；同日期递增；跨日期归 1；``%04d`` 补零。
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
