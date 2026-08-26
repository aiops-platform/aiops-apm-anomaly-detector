"""检测漏斗 pipeline 包。

M4 提供 ``filter_signals``（L1 信号匹配）；M5 落地完整漏斗：
``build_context``（载入规则/动态配置）→ ``run_domain``（L0 抑制 → L1 检测 → L2 关联 → L3 验证 → emit）。
M6 scheduler 编排 collect 后调用。
"""

from aiops_apm.pipeline.context import DetectionContext, DomainResult, build_context, new_trace_id
from aiops_apm.pipeline.emit import emit
from aiops_apm.pipeline.filter_signals import filter_signals
from aiops_apm.pipeline.l0_suppress import l0_suppress
from aiops_apm.pipeline.l1_detect import l1_detect
from aiops_apm.pipeline.l2_correlate import l2_correlate
from aiops_apm.pipeline.l3_verify import l3_verify
from aiops_apm.pipeline.runner import run_domain

__all__ = [
    "DetectionContext",
    "DomainResult",
    "build_context",
    "new_trace_id",
    "filter_signals",
    "l0_suppress",
    "l1_detect",
    "l2_correlate",
    "l3_verify",
    "emit",
    "run_domain",
]
