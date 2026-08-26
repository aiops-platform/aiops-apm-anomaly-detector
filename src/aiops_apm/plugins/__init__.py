"""插件体系：抽象基类（M1 冻结）+ 注册表（M4）。

抽象基类契约在 M1 冻结；registry 通过 entry_points 发现内置/第三方插件，内置插件
在 ``collectors/``（M3）、``detectors/`` / ``suppressors/``（M4）。
"""

from aiops_apm.plugins.base import Collector, Detector, Plugin, Suppressor, build
from aiops_apm.plugins.registry import GROUPS, PluginRegistry

__all__ = ["Plugin", "Collector", "Detector", "Suppressor", "build", "PluginRegistry", "GROUPS"]
