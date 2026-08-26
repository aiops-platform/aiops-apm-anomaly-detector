"""L2 现象摘要钩子：模板默认 + 可插拔 provider（用户确认「模板 + 可插拔」）。

默认 ``TemplateSummaryProvider`` 复用 ``pipeline/l2_correlate.template_summary`` 的
确定性模板（满足「零 LLM 调用」）。``enable_llm_summary`` 为 True 时 M6 仍返回模板
（预留接 LLM 点位，测试用 fake provider 注入）。
"""

from __future__ import annotations

from typing import Any, Protocol

from aiops_apm.pipeline.l2_correlate import template_summary
from aiops_apm.settings import Settings


class SummaryProvider(Protocol):
    """L2 摘要提供者接口（可插拔钩子）。"""

    def summarize(self, *, service: str, metric_anoms: list, log_anoms: list) -> str: ...


class TemplateSummaryProvider:
    """默认确定性模板（复用 template_summary）。"""

    name = "template"

    def summarize(self, *, service: str, metric_anoms: list, log_anoms: list) -> str:
        return template_summary(metric_anoms, log_anoms)


def build_summary_provider(settings: Settings | Any) -> SummaryProvider:
    """按开关返回摘要提供者；M6 不接真实 LLM，一律模板兜底。"""
    # enable_llm_summary=True 预留接 LLM 点位；确定性优先原则下默认模板。
    return TemplateSummaryProvider()
