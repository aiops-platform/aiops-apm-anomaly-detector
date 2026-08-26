"""内置检测器插件包（M4）。entry_points 指向各模块的 ``build()`` 工厂。"""

from aiops_apm.detectors.signature_aggregate import SignatureAggregateDetector
from aiops_apm.detectors.simple_compare import SimpleCompareDetector
from aiops_apm.detectors.static_threshold import Operator, StaticThresholdDetector

__all__ = [
    "Operator",
    "StaticThresholdDetector",
    "SimpleCompareDetector",
    "SignatureAggregateDetector",
]
