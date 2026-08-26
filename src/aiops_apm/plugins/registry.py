"""插件注册表：entry_points 发现 + 原子快照 reload。

设计文档 §5.3：三个 entry_points group（collectors/detectors/suppressors），每个 entry 指向
``build() -> Plugin`` 工厂。``load``/``reload`` 构建新快照后一次 ``MappingProxyType`` 原子替换，
正在执行的轮次继续用旧快照（完成标准「reload 期间跑一轮不抛异常」）。
"""

import importlib.metadata as m
import inspect
import logging
from types import MappingProxyType

from aiops_apm.audit import SecurityAudit
from aiops_apm.exceptions import AppException, ErrorCode
from aiops_apm.plugins.base import Plugin

logger = logging.getLogger(__name__)

GROUPS = {
    "collector": "aiops_apm.collectors",
    "detector": "aiops_apm.detectors",
    "suppressor": "aiops_apm.suppressors",
}


class PluginRegistry:
    """三组插件的注册与查询。``_active`` 是不可变快照，reload 原子替换。"""

    def __init__(self) -> None:
        self._active: MappingProxyType[str, dict[str, Plugin]] = MappingProxyType({k: {} for k in GROUPS})

    def load(self, *, http=None, pool=None, settings=None) -> "PluginRegistry":
        """遍历三组 entry_points，实例化并注册（构建新快照后原子替换）。"""
        snapshot: dict[str, dict[str, Plugin]] = {k: {} for k in GROUPS}
        for kind, group in GROUPS.items():
            for ep in m.entry_points(group=group):
                try:
                    factory = ep.load()
                    plugin = factory(http=http, pool=pool, settings=settings)
                    if inspect.iscoroutine(plugin):  # 契约要求同步 build() -> Plugin；防第三方 async 工厂静默坏
                        logger.warning("plugin build returned coroutine group=%s name=%s (build() 应为同步工厂)", group, ep.name)
                        SecurityAudit.log_plugin_event(ep.name, "load", "failed", detail="async build() not allowed")
                        continue
                    snapshot[kind][ep.name] = plugin
                    SecurityAudit.log_plugin_event(ep.name, "load", "success", detail=f"group={group}")
                except Exception as exc:  # noqa: BLE001 -- 单个插件失败不拖垮整体
                    logger.warning("plugin load failed group=%s name=%s err=%s", group, ep.name, exc)
                    SecurityAudit.log_plugin_event(ep.name, "load", "failed", detail=f"{type(exc).__name__}: {exc}")
        self._active = MappingProxyType(snapshot)
        return self

    def reload(self, *, http=None, pool=None, settings=None) -> "PluginRegistry":
        """重新发现插件（重新扫 entry_points），原子替换为新快照。"""
        return self.load(http=http, pool=pool, settings=settings)

    def register(self, kind: str, name: str, plugin: Plugin) -> None:
        """注入插件（design §5.3；测试/管理用，reload 重新扫 entry_points 会覆盖）。"""
        snapshot = {k: dict(v) for k, v in self._active.items()}
        snapshot.setdefault(kind, {})[name] = plugin
        self._active = MappingProxyType(snapshot)

    def get(self, kind: str, name: str) -> Plugin:
        table = self._active.get(kind, {})
        if name not in table:
            raise AppException(ErrorCode.PLUGIN_NOT_FOUND, f"{kind}/{name}")
        return table[name]

    def list(self, kind: str | None = None) -> dict:
        if kind:
            return {kind: list(self._active.get(kind, {}).keys())}
        return {k: list(v.keys()) for k, v in self._active.items()}
