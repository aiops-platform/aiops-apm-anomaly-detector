"""诊断视图模型：契约与状态常量（纯数据，无 I/O）。

UI（``js/app.js`` 的 ``renderDiagnosis`` / ``.dgx-*``）**只依赖本模块定义的形状**。
字段命名刻意与 UI 现有渲染对齐，避免把"引擎细节"漏进模板。

形状::

    {
      "engine": "agentflow" | "spike",     # 仅调试/排障用，UI 不据此分支
      "status": "analyzing" | "completed" | "failed" | "approved"
                | "closed_manual" | "dismissed",
      "ref": "<run_id 或 session_id>",     # 稳定的单轮标识
      "ref_kind": "run" | "session",
      "session_id": str,                   # = ref（kind 为 session 时）；历史快照同名字段沿用
      "trigger": str,                      # 来源（如 log / manual）
      "created_at": ISO8601 | None,
      "updated_at": ISO8601 | None,
      "issue_title": str,
      "error": str | None,
      "conclusion": {
          root_cause, confidence, confidence_score, summary,
          # confidence        = high|medium|low，供配色/老渲染器
          # confidence_score  = 0~100 整数，档位的同一来源；取不到时 None（如 spike 历史只有档位）
          deployed_commit, base_commit, already_fixed_by, base_note,
          evidence: [...],                 # 证据链（分析链路的一节）
          recommended_fix: [ {title, recommended, applies_when, reason, steps: [...]} ],
      } | None,
      "tasks":     [ {name, status, derived} ],   # 调研计划（分析链路的一节）
      "tool_calls":[ {tool, server, summary} ],   # 工具调用时间线（分析链路的一节）
      "approval":  {available, state, node_id, approver, comment},
      "executions":[ {run_id, status, vm_status, workflow_id,
                      started_at, ended_at, is_current} ],
    }

字段位置刻意与既有渲染器（``renderDiagnosis`` / ``dgxConclusion`` / ``dgxFixOptions`` /
``dgxChain`` / ``dgxTaskView``）**逐字对齐**：方案在 ``conclusion.recommended_fix``、
``tasks`` 与 ``tool_calls`` 在**顶层**、证据链在 ``conclusion.evidence``。

这样历史条目（``diagnose_decision.snapshot``，spike 形状）与实时路径共用同一套渲染，
``dgxChain`` 等纯展示函数一行都不用改——也让"回看历史"和"当时看到的"保持一致。
"""

from __future__ import annotations

from typing import Any

# ── 状态 ──────────────────────────────────────────────────────────────────────
#
# UI 侧 ``PC_DIAG_TERMINAL``（js/app.js）必须与本集合保持一致：到终态即停轮询。

STATUS_ANALYZING = "analyzing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_APPROVED = "approved"
STATUS_CLOSED_MANUAL = "closed_manual"
STATUS_DISMISSED = "dismissed"

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_APPROVED, STATUS_CLOSED_MANUAL, STATUS_DISMISSED}
)

#: agentflow run 状态 → 视图模型状态。
#:
#: ``waiting_approval`` 归到 ``completed``：结论（根因 + 修复计划）此时**已经产出**，
#: run 只是卡在人工审批节点上。UI 的"是否可审批"由 ``approval`` 字段单独表达，
#: 不靠 status 推断——否则「结论已出但待审批」会被误判成"还没跑完"，
#: 用户看不到已经生成好的根因与方案。
_AGENTFLOW_STATUS: dict[str, str] = {
    "queued": STATUS_ANALYZING,
    "running": STATUS_ANALYZING,
    "paused": STATUS_ANALYZING,
    "waiting_approval": STATUS_COMPLETED,
    "success": STATUS_COMPLETED,
    "failed": STATUS_FAILED,
    "cancelled": STATUS_CLOSED_MANUAL,
}

#: agentflow 的「活动」状态（与 ``agentflow/statestore/base.py`` 的 ACTIVE_RUN_STATUSES 同义）。
ACTIVE_RUN_STATUSES: frozenset[str] = frozenset(
    {"queued", "running", "paused", "waiting_approval"}
)


def agentflow_status(run_status: str | None) -> str:
    """agentflow run 状态 → 视图模型状态；未知值保守归 ``analyzing``。

    保守方向是刻意的：未知状态若归终态，UI 会停轮询并把一轮还在跑的诊断显示成"已完成"；
    归 ``analyzing`` 最多是多轮询几次。
    """
    return _AGENTFLOW_STATUS.get((run_status or "").strip(), STATUS_ANALYZING)


def empty_viewmodel(*, engine: str, issue_title: str = "") -> dict[str, Any]:
    """无内容时的骨架（如 run 尚未产出任何节点输出）。"""
    return {
        "engine": engine,
        "status": STATUS_ANALYZING,
        "ref": "",
        "ref_kind": "run" if engine == "agentflow" else "session",
        "session_id": "",
        "trigger": "",
        "created_at": None,
        "updated_at": None,
        "issue_title": issue_title,
        "error": None,
        "conclusion": None,
        "tasks": [],
        "tool_calls": [],
        "approval": {"available": False, "state": "none", "node_id": "", "approver": "", "comment": ""},
        "executions": [],
    }
