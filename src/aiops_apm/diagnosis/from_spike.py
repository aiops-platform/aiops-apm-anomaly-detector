"""spike ``/status/{session_id}`` 快照 → 诊断视图模型。

HolmesGPT 诊断服务的快照**本身就是** UI 渲染器认的形状（``conclusion`` / ``tasks`` /
``tool_calls`` 等字段位置即由它定义），所以这一层几乎是直通：只补上引擎无关的
外层字段（``engine`` / ``ref`` / ``approval`` / ``executions``），并把缺失的键填成空值，
让"老记录"与"新记录"在 UI 侧走**完全相同的渲染路径**。

本模块的存在意义不是转换，而是**给老路径一个与 :mod:`from_agentflow` 并列的位置**——
两个引擎各有适配器，端点按绑定分派，UI 不需要知道区别。
"""

from __future__ import annotations

from typing import Any

from .viewmodel import (
    STATUS_ANALYZING,
    STATUS_APPROVED,
    STATUS_CLOSED_MANUAL,
    STATUS_COMPLETED,
    STATUS_DISMISSED,
    STATUS_FAILED,
    TERMINAL_STATUSES,
    empty_viewmodel,
)

#: spike 会话状态 → 视图模型状态。两者命名本就一致，这里显式列出以防上游加值。
_SPIKE_STATUS: dict[str, str] = {
    "analyzing": STATUS_ANALYZING,
    "completed": STATUS_COMPLETED,
    "failed": STATUS_FAILED,
    "approved": STATUS_APPROVED,
    "closed_manual": STATUS_CLOSED_MANUAL,
    "dismissed": STATUS_DISMISSED,
}

#: ``remediation.status`` → 审批门状态。
_REMEDIATION_STATE: dict[str, str] = {
    "pending_review": "pending",
    "approved": "approved",
    "rejected": "rejected",
    "closed_manual": "closed",
}


def _approval(snap: dict[str, Any]) -> dict[str, Any]:
    """从 ``remediation.status`` 派生审批门状态。

    与 agentflow 侧的差别：spike 的审批是**会话级**（整个 remediation 一个状态），
    不是节点级，故 ``node_id`` 恒为空——UI 只据 ``state`` 决定审批卡片可不可用。
    """
    remediation = snap.get("remediation") if isinstance(snap.get("remediation"), dict) else {}
    raw = str(remediation.get("status") or "")
    state = _REMEDIATION_STATE.get(raw, "none" if not raw else raw)
    return {
        "available": state == "pending",
        "state": state,
        "node_id": "",
        "approver": "",
        "comment": "",
    }


def build(
    snap: dict[str, Any],
    *,
    executions: list[dict[str, Any]] | None = None,
    issue_title: str = "",
) -> dict[str, Any]:
    """把 spike 快照映射成诊断视图模型。"""
    vm = empty_viewmodel(engine="spike", issue_title=issue_title)
    vm["executions"] = executions or []

    session_id = str(snap.get("session_id") or "")
    vm["ref"] = session_id
    vm["ref_kind"] = "session"
    vm["session_id"] = session_id
    vm["trigger"] = str(snap.get("trigger") or "")
    vm["created_at"] = snap.get("created_at")
    vm["updated_at"] = snap.get("updated_at")
    vm["issue_title"] = str(snap.get("issue_title") or issue_title or "")
    vm["error"] = snap.get("error")

    raw_status = str(snap.get("status") or "").strip()
    vm["status"] = _SPIKE_STATUS.get(raw_status, raw_status or STATUS_ANALYZING)

    conclusion = snap.get("conclusion")
    vm["conclusion"] = conclusion if isinstance(conclusion, dict) else None
    vm["tasks"] = [t for t in (snap.get("tasks") or []) if isinstance(t, dict)]
    vm["tool_calls"] = [t for t in (snap.get("tool_calls") or []) if isinstance(t, dict)]
    vm["approval"] = _approval(snap)
    return vm


__all__ = ["TERMINAL_STATUSES", "build"]
