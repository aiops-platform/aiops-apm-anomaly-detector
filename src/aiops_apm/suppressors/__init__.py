"""内置抑制器插件包（M4）。entry_points 指向各模块的 ``build()`` 工厂。"""

from aiops_apm.suppressors.blacklist import BlacklistSuppressor
from aiops_apm.suppressors.maintenance_window import MaintenanceWindowSuppressor

__all__ = ["MaintenanceWindowSuppressor", "BlacklistSuppressor"]
