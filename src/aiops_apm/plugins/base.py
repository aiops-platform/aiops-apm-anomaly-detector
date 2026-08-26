"""插件抽象基类：采集器/检测器/抑制器接口。

契约在 M1 冻结，之后禁止再改签名，只允许增加可选方法/字段。
M3/M4 实现具体插件（entry_points 的 ``build()`` 指向这些接口）。
"""

from abc import ABC, abstractmethod
from typing import Any


class Plugin(ABC):  # noqa: B024 -- 契约冻结的抽象标记基类，抽象方法由三个子类声明
    """所有插件（采集/检测/抑制）的公共基类。"""

    name: str = ""


class Collector(Plugin):
    """采集器：从目标源采集一批原始信号。"""

    @abstractmethod
    async def collect(self, ctx: Any, target: dict) -> list[Any]:
        """采集一批 ``Signal`` 并返回。

        ctx 为 ``DetectionContext``（M5 pipeline 定义），M1 先用 ``Any`` 冻结接口形状。
        """


class Detector(Plugin):
    """检测器：把 ``Signal`` 列表变成 ``Anomaly`` 列表。"""

    @abstractmethod
    async def detect(self, signals: list[Any], params: dict) -> list[Any]:
        """检测并返回异常列表。"""


class Suppressor(Plugin):
    """抑制器：判断信号是否应被抑制（返回抑制原因或 None）。"""

    @abstractmethod
    async def check(self, signal: Any, ctx: Any, params: dict) -> str | None:
        """返回抑制原因；不抑制返回 ``None``。"""

    async def batch_check(self, signals: list[Any], ctx: Any, params: dict) -> list[tuple]:
        """批量抑制检查，默认逐条调用 ``check``（子类可优化）。"""
        return [(s, await self.check(s, ctx, params)) for s in signals]


def build(*, http: Any = None, pool: Any = None, settings: Any = None) -> Plugin:
    """插件工厂（由 entry_points 指向）。M1 占位，M3/M4 实现具体插件时覆盖。"""
    raise NotImplementedError
