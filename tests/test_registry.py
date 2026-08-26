"""PluginRegistry（M4：entry_points 发现 + MappingProxyType 原子快照 reload）。

设计文档 §5.3 + UC-4.1/4.2/4.8；完成标准「reload 期间跑一轮不抛异常」由此覆盖。
"""

import asyncio
from datetime import datetime

import pytest

from aiops_apm.collectors.mock import MockCollector
from aiops_apm.detectors.static_threshold import StaticThresholdDetector
from aiops_apm.exceptions import AppException, ErrorCode
from aiops_apm.models.signal import MetricSignal
from aiops_apm.plugins.registry import GROUPS, PluginRegistry

TS = datetime(2026, 8, 26, 12, 0, 0)


def ms(*, value=0.95) -> MetricSignal:
    return MetricSignal(service="svc-a", metric="cpu_usage", value=value, timestamp=TS)


def test_groups():
    assert GROUPS["collector"] == "aiops_apm.collectors"
    assert GROUPS["detector"] == "aiops_apm.detectors"
    assert GROUPS["suppressor"] == "aiops_apm.suppressors"


def test_load_discovers_builtin_plugins():
    reg = PluginRegistry().load()
    listing = reg.list()
    assert set(listing["collector"]) == {"http_metrics", "http_logs", "mock"}
    assert set(listing["detector"]) == {"static_threshold", "simple_compare", "signature_aggregate"}
    assert set(listing["suppressor"]) == {"maintenance_window", "blacklist"}


def test_get_existing_plugin():
    reg = PluginRegistry().load()
    assert isinstance(reg.get("collector", "mock"), MockCollector)
    assert isinstance(reg.get("detector", "static_threshold"), StaticThresholdDetector)


def test_get_missing_plugin_raises():
    reg = PluginRegistry().load()
    with pytest.raises(AppException) as ei:
        reg.get("detector", "nope")
    assert ei.value.code == ErrorCode.PLUGIN_NOT_FOUND
    assert ei.value.reason == "detector/nope"


def test_get_unknown_kind_raises():
    reg = PluginRegistry().load()
    with pytest.raises(AppException):
        reg.get("bogus_kind", "x")


def test_list_filtered_by_kind():
    reg = PluginRegistry().load()
    assert set(reg.list("detector")["detector"]) == {"static_threshold", "simple_compare", "signature_aggregate"}


def test_register_injects_plugin():
    reg = PluginRegistry().load()
    fake = StaticThresholdDetector()
    reg.register("detector", "custom", fake)
    assert reg.get("detector", "custom") is fake
    assert "custom" in reg.list()["detector"]


def test_active_snapshot_is_immutable_mapping():
    reg = PluginRegistry().load()
    with pytest.raises(TypeError):
        reg._active["detector"] = {}  # type: ignore[index]  # mappingproxy 顶层不可赋值


def test_reload_swaps_to_new_snapshot():
    reg = PluginRegistry().load()
    before = reg.get("detector", "static_threshold")
    reg.reload()
    after = reg.get("detector", "static_threshold")
    assert after is not before  # 新快照新实例
    assert before.name == after.name


@pytest.mark.asyncio
async def test_reload_during_round_does_not_throw():
    """完成标准：reload 期间跑一轮不抛异常（原子快照替换，旧引用继续可用）。"""
    reg = PluginRegistry().load()
    det = reg.get("detector", "static_threshold")

    async def run_round() -> None:
        for _ in range(50):
            await det.detect([ms(value=0.95)], {"threshold": 0.9})

    task = asyncio.create_task(run_round())
    await asyncio.sleep(0)  # 让 round 先跑一拍
    reg.reload()  # 原子替换，正在执行的 round 继续用旧快照
    await task  # 不抛异常


def test_load_failure_isolation(monkeypatch):
    """单插件 build 失败不拖垮整体加载（跳过坏插件，其余正常注册）。"""

    class BadEP:
        name = "bad"

        def load(self):
            raise RuntimeError("boom")

    class GoodEP:
        name = "good"

        def load(self):
            def build(*, http=None, pool=None, settings=None) -> StaticThresholdDetector:
                return StaticThresholdDetector()

            return build

    def fake_entry_points(*, group=None):
        if group == "aiops_apm.detectors":
            return [BadEP(), GoodEP()]
        return []

    monkeypatch.setattr("aiops_apm.plugins.registry.m.entry_points", fake_entry_points)
    reg = PluginRegistry().load()
    assert "good" in reg.list()["detector"]
    assert "bad" not in reg.list()["detector"]
