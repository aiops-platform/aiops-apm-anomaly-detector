"""诊断视图模型：把两个诊断引擎归一到同一条 UI 渲染路径。

Problem Center 的 ``View diagnosis`` 弹窗（``.dgx-*``）历史上直连 HolmesGPT 诊断服务
（spike），后来「分析new」改走 agentflow 工作流 ``problem-log-diagnose``。两者产出的
数据结构不同，若让 UI 自己分辨引擎，等于把后端 agent schema 的知识塞进前端——后端改一次
提示词，前端就静默渲染错乱。

故归一放在这里（API 层的下游、UI 的上游）：

- :mod:`~aiops_apm.diagnosis.viewmodel`    契约与状态常量（纯数据，无 I/O）
- :mod:`~aiops_apm.diagnosis.from_agentflow`  agentflow run → 视图模型
- :mod:`~aiops_apm.diagnosis.from_spike`      spike ``/status`` 快照 → 视图模型

引擎由调用方按问题单 ``evidence`` 里的绑定选择：有 ``agent_run`` 走 agentflow，
有 ``diagnose_session`` 走 spike（老记录）。UI 不需要知道区别。
"""

from __future__ import annotations

from . import from_agentflow, from_spike
from .viewmodel import (
    ACTIVE_RUN_STATUSES,
    STATUS_ANALYZING,
    STATUS_CLOSED_MANUAL,
    STATUS_COMPLETED,
    STATUS_FAILED,
    TERMINAL_STATUSES,
    agentflow_status,
)

__all__ = [
    "ACTIVE_RUN_STATUSES",
    "STATUS_ANALYZING",
    "STATUS_CLOSED_MANUAL",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "TERMINAL_STATUSES",
    "agentflow_status",
    "from_agentflow",
    "from_spike",
]
