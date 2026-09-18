"""agentflow run → 诊断视图模型。

数据源是 agentflow 的 ``GET /runs/{run_id}``（workflow ``problem-log-diagnose``）：

    triage → logs → know → locate → rca → plan → approve-plan → recap

其中 ``rca`` 给根因、``plan`` 给修复计划、``approve-plan`` 是人工审批门。映射见
:func:`build`。工具调用走 ``GET /runs/{run_id}/traces``（``kind=tool_call``）。

**已知能力缺口（有意接受，不是遗漏）**：``problem-log-diagnose`` 只做
「日志分析 → 根因 → 修复计划 → 审批」，**不产出 diff**，故视图模型的
``suggested_diff`` 恒为空；UI 侧已有存在性判断，不会渲染空代码块。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import httpx
from fastapi import Request

from ..collectors._gateway import OutboundGateway
from ..router.deps import get_tenant_id
from .viewmodel import (
    ACTIVE_RUN_STATUSES,
    STATUS_ANALYZING,
    agentflow_status,
    empty_viewmodel,
)

#: 视图模型里代表"根因""修复计划""审批门"的节点 id（与 workflow YAML 对齐）。
NODE_RCA = "rca"
NODE_PLAN = "plan"
NODE_APPROVAL = "approve-plan"

#: 取分析链路时最多回看的工具调用条数（护栏：单轮工具调用可能上千）。
_MAX_TOOL_CALLS = 200

#: 并行拉取历次 run 状态的并发上限（一条问题单的轮次通常个位数）。
_MAX_PARALLEL_RUNS = 8


# ── 出站 ──────────────────────────────────────────────────────────────────────


def _base(request: Request) -> str:
    return str(request.app.state.settings.bug_solve_base_url).rstrip("/")


def _headers(request: Request, tenant: str) -> dict[str, str]:
    """agentflow 侧租户头。两侧租户不同 → 显式桥接，见 ``analyze_problem`` 的注释。"""
    agentflow_tenant = request.app.state.settings.agentflow_tenant or tenant
    return {"X-Tenant-ID": agentflow_tenant}


async def _get(request: Request, path: str, tenant: str) -> httpx.Response | None:
    """向 agentflow GET；**任何失败都返回 None**（读路径不阻断，由调用方降级）。"""
    url = _base(request) + path
    OutboundGateway.validate_url(url)
    http = request.app.state.http_client
    try:
        resp = await http.request("GET", url, headers=_headers(request, tenant))
    except httpx.HTTPError:
        return None
    return resp if resp.status_code < 300 else None


async def fetch_run(request: Request, run_id: str) -> dict[str, Any] | None:
    """拉一次 run 详情；失败/非 2xx 返回 None。"""
    resp = await _get(request, "/runs/" + run_id, get_tenant_id(request))
    if resp is None:
        return None
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


async def fetch_traces(request: Request, run_id: str) -> list[dict[str, Any]]:
    """拉该 run 的节点 traces（工具调用 / LLM 调用）；失败返回空表。"""
    resp = await _get(
        request, f"/runs/{run_id}/traces?limit={_MAX_TOOL_CALLS}", get_tenant_id(request)
    )
    if resp is None:
        return []
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []


async def fetch_executions(request: Request, run_entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把问题单里的 ``agent_run`` evidence 列表补上**实时状态**，供 UI 的「历次执行」区展示。

    每条 run 都要问一次 agentflow，故并发拉取并设并发上限；单条失败只丢该条的状态
    （``vm_status`` 留空），不拖垮整个列表。
    """
    entries = list(run_entries)
    if not entries:
        return []

    sem = asyncio.Semaphore(_MAX_PARALLEL_RUNS)

    async def one(entry: dict[str, Any]) -> dict[str, Any]:
        run_id = str(entry.get("run_id") or "")
        item: dict[str, Any] = {
            "run_id": run_id,
            "status": "",          # agentflow 原始状态
            "vm_status": "",       # 视图模型状态
            "workflow_id": entry.get("workflow_id") or "",
            "started_at": _iso(entry.get("started_at")),
            "ended_at": None,
            "is_current": False,
        }
        if not run_id:
            return item
        async with sem:
            run = await fetch_run(request, run_id)
        if run:
            raw = str(run.get("status") or "")
            item["status"] = raw
            item["vm_status"] = agentflow_status(raw)
            item["ended_at"] = _iso(run.get("updated_at"))
        return item

    items = await asyncio.gather(*(one(e) for e in entries))
    # 当前轮 = 列表里最后一轮（evidence 按写入顺序追加，重跑追加在尾部）
    if items:
        items[-1]["is_current"] = True
    return items


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


# ── 映射 ──────────────────────────────────────────────────────────────────────


def _confidence_chip(value: Any) -> str:
    """数值置信度（0~1）→ UI 的 ``high``/``medium``/``low`` 档位。

    UI 的 ``dgxConfChip`` 只认档位字符串；直接用数值会落到 ``medium`` 兜底分支，
    等于把「模型很确信」和「模型没把握」渲染成同一个样子。
    """
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("high", "medium", "low"):
            return low
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "medium"
    if num >= 0.8:
        return "high"
    if num >= 0.5:
        return "medium"
    return "low"


def _confidence_score(value: Any) -> int | None:
    """置信度原始分值 → UI 的整百分数（0~100）；取不到 / 不是数值 → ``None``。

    上游 ``rca.confidence`` 是 0~1 的小数（实测 0.86）。``_confidence_chip`` 把它压成
    high/medium/low 三档后**原值就丢了** —— 审批人分不出「0.86」和「0.51」，而这两者
    对「敢不敢直接放行」的意义完全不同。这里按同一份输入另存一个百分数：
    档位与分值是同一个数的两种呈现，档位继续供配色与老渲染器使用。

    已经是 high/medium/low 档位串（spike 形状）时返回 ``None`` —— 那是没有分值的，
    如实留空，不要编一个数出来。
    """
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if 0 <= num <= 1:  # 0~1 比例 → 百分数
        num *= 100
    if not 0 <= num <= 100:  # 超出量纲（如把 5 当 5% 还是 0.05 无从判断）→ 宁可不显示
        return None
    return round(num)


def _node_output(run: dict[str, Any], node_id: str) -> dict[str, Any]:
    nodes = run.get("nodes")
    if not isinstance(nodes, dict):
        return {}
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        return {}
    out = node.get("output")
    return out if isinstance(out, dict) else {}


def _plan_payload(plan_out: dict[str, Any]) -> dict[str, Any]:
    """``fix-planner`` 的输出是 ``{"plan": {summary, steps}}`` —— 实际内容**多包了一层**。

    这里拆掉那一层；若将来换成直接平铺的 schema（如 ``remediation-planning-analyst``
    的 ``REMEDIATION_PLANNING_SCHEMA``，其 summary/steps 在顶层），也能照常工作。
    """
    inner = plan_out.get("plan")
    return inner if isinstance(inner, dict) else plan_out


def _primary_root_cause(rca: dict[str, Any]) -> str:
    """``RootCauseSchema`` **没有 summary 字段**，根因结论在 ``hypotheses`` 里。

    取第一条——按该 schema 的约定，模型把最可信的假设排在首位（``ruled_out`` 是已排除项）。
    兜底退回 ``root_cause_type``，至少让用户看到定性分类而不是空白。
    """
    hypotheses = [str(h).strip() for h in (rca.get("hypotheses") or []) if str(h).strip()]
    if hypotheses:
        return hypotheses[0]
    return str(rca.get("root_cause_type") or "").strip()


def _build_conclusion(
    rca: dict[str, Any], plan: dict[str, Any], evidence: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """根因 + 总结 + 方案 + 证据链。

    ``recommended_fix`` 与 ``evidence`` 刻意放在 ``conclusion`` 里——位置与既有渲染器
    （``dgxFixOptions(c.recommended_fix)`` / ``dgxChain`` 读 ``snap.conclusion.evidence``）
    及历史快照逐字一致。
    """
    root_cause = _primary_root_cause(rca)
    summary = str(plan.get("summary") or "").strip()
    options = _build_options(plan)
    if not root_cause and not summary and not options:
        return None
    ref = plan.get("root_cause_ref") if isinstance(plan.get("root_cause_ref"), dict) else {}
    return {
        "root_cause": root_cause or str(ref.get("summary") or "—"),
        "confidence": _confidence_chip(rca.get("confidence")),
        # 档位供配色与老渲染器；分值供人读数（见 _confidence_score 的说明）。
        # 两者出自同一个 rca.confidence，不会打架。
        "confidence_score": _confidence_score(rca.get("confidence")),
        # 工作流不产出 commit 元信息（base/deployed/已修复于）——留空，UI 自动不渲染。
        "deployed_commit": "",
        "base_commit": "",
        "already_fixed_by": "",
        "base_note": "",
        "summary": summary,
        "evidence": evidence,
        "recommended_fix": options,
    }


def _map_step(step: dict[str, Any]) -> dict[str, Any]:
    """``steps[]`` → UI 的 step 形状。

    字段名对齐渲染器 ``dgxOptionBody``（``expected_effect`` 而非 ``expected``），
    避免为了映射反过来去改渲染器。上游 schema
    （``prompts.py`` 的 ``_REMEDIATION_STEP_SCHEMA``）现已按同一套键名产出，
    所以这里基本是 1:1；``expected`` 的兼容分支留给老快照。缺键就是空串，
    UI 逐个判空、不渲染空块。
    """
    return {
        "action": str(step.get("action") or ""),
        "target": str(step.get("target") or ""),
        "type": str(step.get("type") or ""),  # code_fix / infra_action / config_change
        "risk": str(step.get("risk") or ""),
        "phase": str(step.get("phase") or ""),
        "scope": str(step.get("scope") or ""),
        "change": str(step.get("change") or ""),
        "expected_effect": str(step.get("expected") or step.get("expected_effect") or ""),
        "verification": str(step.get("verification") or ""),
        "rollback": str(step.get("rollback") or ""),
        "requires_approval": bool(step.get("requires_approval")),
        # 工作流不产出 diff（见模块 docstring 的能力缺口说明）。
        "suggested_diff": "",
    }


def _build_options(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """修复方案。

    - ``plan.decisions[]`` 非空 → **每个决策的每个 option 一个方案页签**，各取
      ``opt.steps``（该选项自己的完整计划）；模型漏给时回退 ``plan.steps``。
      ``recommended`` 取自决策的 ``recommended`` 字段。多个决策时标题前缀决策问题，
      否则光看选项标题分不清在选什么。
    - 无决策 → **单个方案**（就是这份计划本身），标为推荐。

    各选项**必须有各自的 steps**：共用一份会让页签切换毫无意义——点开哪个正文都一样，
    实测正是这么踩的（6 个选项共用同一份 5 步计划，哈希全等）。
    """
    steps = [_map_step(s) for s in (plan.get("steps") or []) if isinstance(s, dict)]
    decisions = [d for d in (plan.get("decisions") or []) if isinstance(d, dict)]
    if not decisions:
        if not steps:
            return []
        return [
            {
                "title": str(plan.get("summary") or "(未命名方案)"),
                "recommended": True,
                "applies_when": "",
                "reason": "",
                "steps": steps,
            }
        ]

    multi = len(decisions) > 1
    options: list[dict[str, Any]] = []
    for decision in decisions:
        question = str(decision.get("question") or "").strip()
        recommended_id = str(decision.get("recommended") or "")
        for opt in decision.get("options") or []:
            if not isinstance(opt, dict):
                continue
            title = str(opt.get("title") or "(未命名方案)")
            if multi and question:
                title = f"{question} · {title}"
            reason_parts = [str(opt.get("description") or "").strip()]
            pros = [str(p) for p in (opt.get("pros") or []) if str(p).strip()]
            cons = [str(c) for c in (opt.get("cons") or []) if str(c).strip()]
            if pros:
                reason_parts.append("优点：" + "；".join(pros))
            if cons:
                reason_parts.append("缺点：" + "；".join(cons))
            # 每个选项自己的完整计划（按该选项方向展开）。回退顶层 ``plan.steps`` 是给
            # 老形状/模型偶尔漏给用的——**不要**反过来把顶层当成常态，否则又退化成
            # 「N 个选项共用一份计划、点开哪个都一样」。
            option_steps = [_map_step(s) for s in (opt.get("steps") or []) if isinstance(s, dict)]
            options.append(
                {
                    "title": title,
                    "recommended": bool(recommended_id) and str(opt.get("id") or "") == recommended_id,
                    "applies_when": str(decision.get("accept_criteria") or ""),
                    "reason": " ".join(p for p in reason_parts if p),
                    "steps": option_steps or steps,
                }
            )
    return options


#: run 节点状态 → ``dgxTaskView`` 认的任务状态。
_TASK_STATUS: dict[str, str] = {
    "done": "done",
    "running": "in_progress",
    "pending": "todo",
    "waiting_approval": "in_progress",
    "failed": "failed",
    "skipped": "cancelled",
    "rejected": "cancelled",
    "rejected-canceled": "cancelled",
}


#: 取数节点 → 证据链里的来源标签。
_EVIDENCE_NODES: tuple[tuple[str, str], ...] = (
    ("logs", "日志证据"),
    ("know", "历史知识"),
    ("locate", "代码定位"),
)


#: ``problem-log-diagnose`` 各节点的中文标签。
#:
#: ``GET /runs/{id}`` 的节点字典**不含** ``name``（workflow YAML 里的 name 没随 run 快照出来），
#: 不映射的话页面上直接印节点 id（``approve-plan``/``know``…），对使用者没有意义。
#: 节点上的 ``name`` 若存在则优先用它。
_NODE_LABELS: dict[str, str] = {
    "triage": "症状分类",
    "logs": "日志证据",
    "know": "历史知识",
    "locate": "代码定位",
    "rca": "根因分析",
    "plan": "修复计划",
    "approve-plan": "审核修复计划",
    "recap": "复盘",
}


def _build_tasks(run: dict[str, Any]) -> list[dict[str, Any]]:
    """调研计划 ← run 的各节点。``dgxTaskView`` 复用同一套中文状态标签渲染。

    spike 侧 ``tasks`` 是模型自报账本，这里换成**引擎实际执行的节点**——更可信：
    节点状态是调度器写的，不是模型自称的。

    ⚠️ 字段名是 ``title``（渲染器读 ``t.title``），不是 ``name``——写成 ``name``
    不报错、只是每条任务的标题渲染成空白。
    """
    nodes = run.get("nodes") if isinstance(run.get("nodes"), dict) else {}
    return [
        {
            "title": str(node.get("name") or _NODE_LABELS.get(node_id) or node_id),
            "status": _TASK_STATUS.get(str(node.get("status") or ""), "todo"),
            "derived": False,
        }
        for node_id, node in nodes.items()
        if isinstance(node, dict)
    ]


#: trace 的 ``payload.result_state`` → 渲染器的 ``status``（ok / error / 运行中）。
_TOOL_STATE: dict[str, str] = {
    "success": "ok",
    "ok": "ok",
    "error": "error",
    "failed": "error",
}


def _build_tool_calls(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """工具调用时间线 ← traces 里 ``kind=tool_call`` 的条目。

    ``name`` 对 MCP 调用**本来就带** ``mcp__{server}__{tool}`` 前缀，直接透传即可——
    渲染器的 ``dgxToolParts`` 靠这个前缀分组「使用的 MCP Server」，剥掉前缀会让那一节整块消失。

    字段名（``status``/``args``/``result_summary``）同样是渲染器定的；``payload`` 里
    对应的分别是 ``result_state``/``input``/``result``。
    """
    calls: list[dict[str, Any]] = []
    for t in traces:
        if str(t.get("kind") or "") != "tool_call":
            continue
        payload = t.get("payload") if isinstance(t.get("payload"), dict) else {}
        args = payload.get("input")
        result = payload.get("result")
        calls.append(
            {
                "tool": str(t.get("name") or ""),
                "node_id": str(t.get("node_id") or ""),
                "status": _TOOL_STATE.get(str(payload.get("result_state") or ""), "run"),
                "args": args if isinstance(args, dict) else {},
                "result_summary": str(result or "")[:500],
            }
        )
    return calls[:_MAX_TOOL_CALLS]


def _build_evidence(run: dict[str, Any]) -> list[dict[str, Any]]:
    """证据链 ← ``logs`` / ``know`` / ``locate`` 三个取数节点的输出。

    ⚠️ 结论字段名是 ``finding``（渲染器读 ``it.finding``），不是 ``conclusion``。
    """
    evidence: list[dict[str, Any]] = []
    for node_id, label in _EVIDENCE_NODES:
        out = _node_output(run, node_id)
        if not out:
            continue
        text = str(out.get("summary") or out.get("conclusion") or "").strip()
        if not text:
            continue
        evidence.append(
            {
                "source": label,
                "query": node_id,
                "finding": text[:2000],
                "supporting_text": "",
            }
        )
    return evidence


def _build_approval(run: dict[str, Any]) -> dict[str, Any]:
    """审批门状态。``available`` 只在 run 真的卡在该节点上时为真。"""
    pending = [p for p in (run.get("pending_approvals") or []) if isinstance(p, dict)]
    node_id = ""
    for p in pending:
        if str(p.get("node_id") or "") == NODE_APPROVAL:
            node_id = NODE_APPROVAL
            break
    if not node_id and pending:
        node_id = str(pending[0].get("node_id") or "")
    state = "pending" if node_id else "none"
    output = _node_output(run, NODE_APPROVAL)
    if not node_id and output:
        approved = output.get("approved")
        state = "approved" if approved is True else ("rejected" if approved is False else "none")
    return {
        "available": bool(node_id),
        "state": state,
        "node_id": node_id,
        "approver": str(output.get("approver") or ""),
        "comment": str(output.get("comment") or ""),
    }


def build(
    run: dict[str, Any],
    *,
    traces: list[dict[str, Any]] | None = None,
    executions: list[dict[str, Any]] | None = None,
    issue_title: str = "",
) -> dict[str, Any]:
    """把一次 agentflow run 映射成诊断视图模型。"""
    vm = empty_viewmodel(engine="agentflow", issue_title=issue_title)
    vm["executions"] = executions or []

    run_id = str(run.get("run_id") or "")
    vm["ref"] = run_id
    vm["ref_kind"] = "run"
    # 历史快照用 session_id 存这一轮的标识；实时路径沿用同名字段承接 run_id，
    # 让 UI 的状态头不必为引擎分支（``ref_kind`` 供提示文案区分）。
    vm["session_id"] = run_id
    vm["trigger"] = str(run.get("workflow") or "")
    vm["status"] = agentflow_status(run.get("status"))
    vm["created_at"] = _iso(run.get("created_at"))
    vm["updated_at"] = _iso(run.get("updated_at"))

    # 节点级失败原因优先暴露——run 失败但节点有 error 时，只说"失败"等于把线索丢掉。
    errors = [
        str(node.get("error"))
        for node in (run.get("nodes") or {}).values()
        if isinstance(node, dict) and node.get("error")
    ]
    if errors:
        vm["error"] = errors[0][:2000]

    rca = _node_output(run, NODE_RCA)
    plan = _plan_payload(_node_output(run, NODE_PLAN))
    vm["tasks"] = _build_tasks(run)
    vm["tool_calls"] = _build_tool_calls(traces or [])
    vm["conclusion"] = _build_conclusion(rca, plan, _build_evidence(run))
    vm["approval"] = _build_approval(run)
    return vm


def build_unavailable(*, issue_title: str = "", error: str = "") -> dict[str, Any]:
    """run 拉不到时的降级视图（不抛错：读路径失败不该让弹窗整个打不开）。"""
    vm = empty_viewmodel(engine="agentflow", issue_title=issue_title)
    vm["status"] = STATUS_ANALYZING
    vm["error"] = error or "分析运行信息暂时读取不到（agentflow 不可达或 run 已过期）"
    return vm


__all__ = [
    "NODE_APPROVAL",
    "NODE_PLAN",
    "NODE_RCA",
    "ACTIVE_RUN_STATUSES",
    "build",
    "build_unavailable",
    "fetch_executions",
    "fetch_run",
    "fetch_traces",
]
