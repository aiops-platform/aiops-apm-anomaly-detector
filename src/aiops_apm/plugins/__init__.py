"""插件体系：抽象基类 + 注册表。

M1 仅冻结抽象基类（``plugins/base.py``）；registry 与内置插件属 M4。
"""

from aiops_apm.plugins.base import Collector, Detector, Plugin, Suppressor, build

__all__ = ["Plugin", "Collector", "Detector", "Suppressor", "build"]
