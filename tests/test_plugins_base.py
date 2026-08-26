"""UC-1.4 插件契约校验。

合法 Detector 子类可实例化并调用；缺 ``detect()`` 的非法子类实例化抛 ``TypeError``。
"""

import pytest

from aiops_apm.plugins.base import Detector, Plugin, Suppressor, build


class MyDetector(Detector):
    name = "my_detector"

    async def detect(self, signals, params):
        return signals


async def test_valid_detector_instantiates_and_calls() -> None:
    d = MyDetector()
    assert isinstance(d, Plugin)
    assert d.name == "my_detector"
    assert await d.detect([1, 2], {}) == [1, 2]


def test_missing_detect_raises_type_error() -> None:
    class BadDetector(Detector):  # 缺 detect()，抽象方法未实现
        pass

    with pytest.raises(TypeError):
        BadDetector()


class PassSuppressor(Suppressor):
    name = "pass"

    async def check(self, signal, ctx, params):
        return None


async def test_suppressor_batch_check_default() -> None:
    s = PassSuppressor()
    assert await s.batch_check(["a", "b"], None, {}) == [("a", None), ("b", None)]


def test_build_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        build()
