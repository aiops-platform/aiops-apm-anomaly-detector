"""Mock 采集器：返回预定义信号，供演示/单测（不走网络、不写库）。"""

from __future__ import annotations

from typing import Any

from ..plugins.base import Collector


class MockCollector(Collector):
    """返回 ``target["_mock_signals"]`` 的采集器。"""

    name = "mock"

    async def collect(self, ctx: Any, target: dict) -> list:
        """返回预定义信号列表。"""
        return list(target.get("_mock_signals", []))


def build(*, http: Any = None, pool: Any = None, settings: Any = None) -> Collector:
    """插件工厂（entry_points 指向）。"""
    return MockCollector()
