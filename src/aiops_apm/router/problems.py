"""UC-6.4：``/v1/problems`` 问题单查询与手动关闭。M7（UC-7.6）resolve 支持误报回写。

另含「分析new」诊断链路：``POST /{id}/diagnose`` 发起 + ``GET /{id}/diagnose`` 读快照，
以及计划级审批 ``POST /{id}/diagnose/decision``（拒绝重跑 / 忽略关单 / 误报关单）与
``GET /{id}/diagnose/decisions``（历史计划，落本仓 evidence）。

历史条目除决策摘要外还带一份 ``snapshot``——**决策那一刻**从诊断服务 ``/status`` 抓到的那一轮快照
（结论/方案/分析链路）。必须抓在**动作之前**：拒绝会立刻后台重跑并推平整轮现场
（spike ``runner.py:117-121`` 清空 raw_reply/tasks/tool_calls/conclusion/remediation），事后取不到。
该字段只在详情与 ``/diagnose/decisions`` 返回，
**列表响应里被剥离**（``_strip_decision_snapshots``）——列表是 5s 轮询 + limit=500，背不动大包体。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..collectors._gateway import OutboundGateway
from ..diagnosis import from_agentflow, from_spike
from ..exceptions import AppException, ErrorCode
from ..metrics import update_fpr_gauge
from .deps import get_tenant_id

router = APIRouter(prefix="/v1/problems", tags=["problems"])

# severity → (impact, urgency, priority)，ServiceNow 1 = 最高
_SEV_MAP = {
    "critical": ("1", "1", "1"),
    "high": ("2", "2", "2"),
    "warning": ("3", "3", "3"),
}

#: 问题单的终态：落了这几档就不能再发起分析 / 诊断 / 裁定。
#:
#: ⚠️ 收成一个常量是防漏：这三个字面量原本**复制了三份**（analyze / diagnose / decision
#: 各一处），加 `escalated` 时只改两处就会留下一个能对已升级的单重跑诊断的入口。
#: `archived` 在词表里但全仓没有任何写入方，留着是防御性的。
_TERMINAL_STATES = ("resolved", "closed", "archived", "escalated")

#: 起 agentflow run 超时的统一文案（analyze 与 rerun 两处共用，同 ``_TERMINAL_STATES`` 的防漏理由）。
#:
#: **必须写明"run 可能已经在后台跑"**：agentflow 的 ``POST /run`` 在返回前同步准备工作区，
#: 超时只表示我们不等了，它那边照建照跑。措辞若只说"失败"，用户就会重试 —— 而每次重试
#: 都会**再起一个 run**（实测：66 秒内点了三次 → 三个 run，我们这边一个都不认识）。
_RUN_START_TIMEOUT_REASON = (
    "agent workflow run start timed out after {secs:g}s; agentflow prepares the fix-side workspace "
    "synchronously before returning, so the run MAY ALREADY BE RUNNING in the background — check this "
    "problem's evidence/run before retrying, because a retry starts ANOTHER run"
)

def _primary_service(rec: dict) -> str:
    """记录的主服务名（单个）。

    M9 起跨服务记录的 ``service`` 是逗号拼接串（如 ``"gateway-service,order-service"``），
    而下游有些字段只能放**一个**服务名——诊断服务的 ``app``/``repo`` 路由、
    ServiceNow 的 ``cmdb_ci.name`` 都是单值。取拼接串的第一个（emit 时按服务名排序，
    故第一个是确定性的组代表）。

    展示类文案（``_symptom``/``_ticket_description``）**不要**用这个——那里列出全部
    服务反而更有信息量，直接用原始 ``service``。
    """
    return (rec.get("service") or "").split(",")[0]


def _detection_type(rec: dict) -> str:
    """检测来源：``log`` / ``metric`` / ``combined`` / ``unknown``。

    ``problem_record`` **没有**显式的类型列——``source`` 是模块名（固定 ``apm-alert``），
    与检测来源无关。类型由证据本身决定：

    ==================  ==================  ============
    metric_anomalies    log_anomalies       结果
    ==================  ==================  ============
    空                  非空                ``log``
    非空                空                  ``metric``
    非空                非空                ``combined``（L2 同源关联命中，可能已升 critical）
    空                  空                  ``unknown``（理论上不该出现）
    ==================  ==================  ============

    刻意**派生而非入库**：存列会与证据漂移（改了 anomaly 忘了同步列），派生不可能不一致，
    也不需要迁移和历史回填。``correlation.reason`` 携带同样的事实，但它的取值
    （``log_only`` / ``metric_only`` / ``metric_log_within_window`` / ``unrelated``）语义偏
    "关联结果"，前端做类型筛选不如这个直白。
    """
    has_metric = bool(rec.get("metric_anomalies"))
    has_log = bool(rec.get("log_anomalies"))
    if has_metric and has_log:
        return "combined"
    if has_log:
        return "log"
    if has_metric:
        return "metric"
    return "unknown"


class AnalyzeProblemBody(BaseModel):
    workflow_id: str
    # 打回后重跑：state=in_progress 时默认 409（防重复发起），显式 ``rerun=true`` 才再起一轮。
    # 引擎没有"重跑同一个 run"的语义，重跑 = 起一个新 run 并把绑定追加进 evidence（读取取最后一条）。
    rerun: bool = False


@router.get("")
async def list_problems(
    request: Request,
    state: str | None = None,
    service: str | None = None,
    severity: str | None = None,
    limit: int = 50,
) -> dict:
    """按租户列问题单，可选 state / service / severity 过滤，detected_at 倒序。

    列表**剥离** ``diagnose_decision.snapshot``——该字段是整轮诊断快照（可达数十 KB），
    而列表被前端每 5s 轮询一次且 limit 拉到 500，逐条回传会白背大包体；
    列表侧只用到 evidence 判"有没有绑诊断/有没有 agent run"，不需要快照内容。
    """
    tenant = get_tenant_id(request)
    items = await request.app.state.storage.records.list(
        tenant, state=state, service=service, severity=severity, limit=limit
    )
    # 注意构造**新 dict**：``_strip_decision_snapshots`` 在无需剥离时返回的是同一个对象，
    # 而 InMemory store 的 list() 给的就是库内 dict —— 原地改会污染存储。
    return {"items": [{**_strip_decision_snapshots(r), "detection_type": _detection_type(r)} for r in items]}


def _strip_decision_snapshots(rec: dict) -> dict:
    """列表专用：把 ``diagnose_decision`` 条目里的 ``snapshot`` 剥掉（浅拷贝，不改原记录）。"""
    decisions = [e for e in rec.get("evidence") or [] if isinstance(e, dict) and e.get("type") == "diagnose_decision"]
    if not decisions:
        return rec
    slim = {k: v for k, v in rec.items() if k != "evidence"}
    slim["evidence"] = [
        ({k: v for k, v in e.items() if k != "snapshot"}
         if isinstance(e, dict) and e.get("type") == "diagnose_decision"
         else e)
        for e in rec.get("evidence") or []
    ]
    return slim


@router.get("/{record_id}")
async def get_problem(request: Request, record_id: str) -> dict:
    """单条问题单详情。"""
    tenant = get_tenant_id(request)
    rec = await request.app.state.storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")
    # 新 dict：详情同样不能原地改库内对象（见 list_problems 的说明）
    return {**rec, "detection_type": _detection_type(rec)}


async def _record_fpr(storage, tenant: str, rec: dict, *, false_positive: bool) -> bool:
    """把该单 ``group_key`` 记一次判定（``false_positive=True`` 记误报）。

    写回 ``fpr_table``（total+1，fpr 重算）并刷新 ``aiops_false_positive_rate`` Gauge。
    返回是否真正记录（无 group_key → False）。解析逻辑：``False`` = 有效判定（非误报）。
    """
    group_key = rec.get("group_key")
    if not group_key:
        return False
    await storage.dynamic_config.write_fpr(tenant, group_key, false_positive=false_positive)
    fpr_data = await storage.dynamic_config.load_fpr(tenant)
    update_fpr_gauge(
        tenant,
        rec.get("domain", "application"),
        rec.get("service", "unknown"),
        fpr_data,
    )
    return True


@router.post("/{record_id}/resolve")
async def resolve_problem(request: Request, record_id: str, body: dict | None = None) -> dict:
    """手动关闭问题单（reason=manual）→ ``state=resolved``（已修复/已处理）。

    M7（UC-7.6）：可选 body ``{"false_positive": true}``——为真时把该单 ``group_key``
    记为一次误报，写回 ``fpr_table``（total+1，fpr 重算）并更新 ``aiops_false_positive_rate`` Gauge。
    body 缺省 / 为假 → 记为一次有效判定（非误报）。

    「忽略」（人判定不做）**不走这里**，走 ``POST /{record_id}/ignore``——两者是并列的终态，
    混用的后果见该端点的说明。
    """
    false_positive = bool((body or {}).get("false_positive", False))
    tenant = get_tenant_id(request)
    storage = request.app.state.storage
    rec = await storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")

    recorded = await _record_fpr(storage, tenant, rec, false_positive=false_positive)

    await storage.records.resolve(tenant, record_id, reason="manual")
    return {
        "record_id": record_id,
        "state": "resolved",
        "false_positive_recorded": recorded,
    }


@router.post("/{record_id}/ignore")
async def ignore_problem(request: Request, record_id: str) -> dict:
    """手动「忽略」问题单 → ``state=closed``（reason=``ignored``），与 ``resolved`` 并列的终态。

    ``resolved`` = 已修复/已处理，``closed`` = 人判定不做（忽略）；两者复用同一组审计列
    ``resolved_at``/``resolve_reason``（通用的「关闭时间/原因」，非 resolved 专属）。

    **刻意与 ``/resolve`` 分开，不能合并成「都是关单」**：

    - 忽略**不是**误报——本端点**不**回写 ``fpr_table``。走 ``/resolve {"false_positive": true}``
      才记误报；否则每点一次「忽略」（= 先搁置不看）都会给该 ``group_key`` 记一次误报，
      L3 的误报率闸门与 ``aiops_false_positive_rate`` Gauge 被污染。
    - 语义上也要分开：界面上说「忽略」却在库里写 ``resolved``，等于替用户编造「已修复」的结论。

    **不去动已绑定的诊断会话**（``/dismiss`` 收尾由 ``POST /{id}/diagnose/decision`` 做）：
    本端点服务的是列表行上的「忽略」，对**没绑过诊断**的单同样可用，故不能依赖会话存在。
    """
    tenant = get_tenant_id(request)
    storage = request.app.state.storage
    rec = await storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")

    await storage.records.close(tenant, record_id, reason="ignored")
    return {"record_id": record_id, "state": "closed"}


class EscalateBody(BaseModel):
    """直接派单的载荷。

    ``workflow_name`` = 这张单**后续跑哪条流程**（建单时就钉死）。空 = 不钉，
    发起时退回库里最新一条（agentflow ``_workflow_for_ticket`` 的第三档）。

    ⚠️ 传**名字**不传 id：``workflows.id`` 每个租户库都不同，名字才是可移植的键
    （与 workflow YAML 里 ``next_workflow`` 的字面量同一条约定）。本仓按名字查回 id
    再交给 agentflow——``POST /tickets`` 只收 id。
    """

    workflow_name: str = ""


@router.post("/{record_id}/escalate")
async def escalate_problem(
    request: Request, record_id: str, body: EscalateBody | None = None
) -> dict:
    """把问题单**直接**派成一张修复工单（本仓取号 + 绑定 + 转 ``escalated`` 终态）。

    与 ``POST /{id}/diagnose/decision {decision: "escalate"}`` 的区别是**它不经过诊断裁定**：

    - 那条路要有一轮诊断、要放行 agentflow 的审批门，工单由 run 里的 ``kind: ticket``
      节点建出、跑哪条流程由 YAML 的 ``next_workflow`` 说了算；
    - 这条给**没诊断过 / 诊断没给出方案**的单用（页面上两处「Escalate」），
      由调用方指定流程。

    **两者产出的 evidence 是同一个形状**（``diagnose_decision`` + ``decision=escalate``
    + ``ticket_id``/``ticket_number``）——「升级」与「它建出的那张工单」是同一件事，
    拆成两种条目会让"读的人自己把两条拼起来"（见 ``_created_ticket``）。
    区别只在 ``engine``：``manual`` = 人直接派的，没有诊断轮次，故**不写** ``session_id``
    （前端的「历史计划」据此不再打印"该轮快照不可用"——那轮根本不存在）。

    **派单方仍是本仓**：号由 ``SequenceStore`` 取，绑定写进 evidence，回传（``/ticket-status``）
    才能按号反查回来。绕过本仓直接在 agentflow 建单的话，那张单既没有号、也不绑问题单，
    修完的回传会查无此单。
    """
    tenant = get_tenant_id(request)
    storage = request.app.state.storage
    rec = await storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")

    # 幂等守卫放在终态守卫**之前**：已经派过单的记录，这句话比"is escalated"有用得多
    # （它直接给出是哪个号）。`_created_ticket` 认的正是升级那一步写的 evidence。
    existing = _created_ticket(rec)
    if existing is not None:
        held = existing.get("ticket_number") or existing.get("ticket_id") or ""
        raise AppException(
            ErrorCode.CONFLICT, f"problem {record_id} already holds ticket {held}"
        )
    if (rec.get("state") or "pending") in _TERMINAL_STATES:
        raise AppException(
            ErrorCode.CONFLICT,
            f"problem {record_id} is {rec.get('state')}; escalate not allowed",
        )

    name = str((body.workflow_name if body else "") or "").strip()
    workflow_id = await _resolve_workflow_id(request, name)

    created = await _agentflow_create_ticket(
        request, tenant, rec, None, workflow_id=workflow_id
    )
    recorded: dict = {
        "type": "diagnose_decision",
        "decision": "escalate",
        "engine": "manual",
        "ticket_id": created["ticket_id"],
        "ticket_number": created["ticket_number"],
        "workflow_name": name or None,
        "decided_at": datetime.now(timezone.utc),
    }
    # ⚠️ evidence 必须在**终态之前**落（顺序见 `_apply_escalated_state`）：反过来的话
    # 重试时幂等判据（`_created_ticket` 读 evidence）不成立 → 建出第二张工单。
    await storage.records.append_evidence(tenant, record_id, recorded)
    await _apply_escalated_state(storage, tenant, record_id, "escalate", recorded)
    return {
        "record_id": record_id,
        "state": "escalated",
        "ticket_id": created["ticket_id"],
        "ticket_number": created["ticket_number"],
        "workflow_name": name,
    }


# ── 工单回传：agentflow 修完之后把工单状态送回来 ──────────────────────────────
#
# 链路：本仓 escalate 派单（`_agentflow_create_ticket`）→ agentflow 跑修复工作流
# → `returnApmTicketStatus`（MCP 写工具）POST 回这里。
#
# **这是本仓唯一由外部系统（而非人）驱动的工单写面**，所以幂等必须自己做：
# agentflow 侧的节点是幂等节点（`SIDE_EFFECT_AGENTS`，键 `run_id:node_id`），
# 但它 resume / 换轮次时仍可能重放同一次回传，而 `append_evidence` 是无条件追加。

#: 回传状态 → 问题单动作。
#:
#: **只有 `resolved` 改问题单状态**：那是"修复已落地"，问题真的没了。另外两个
#: （`failed` 修复没走完 / `insufficient` 没定位到可改的东西）都是"还没修好"——
#: 单子仍该挂 `escalated` 等人处理，把它改成别的状态等于**替现场编造一个结论**。
_TICKET_RESOLVED = "resolved"


class TicketStatusBody(BaseModel):
    """agentflow `returnApmTicketStatus` 回传的载荷。

    ⚠️ ``ticket_id`` 装的是**本仓派出的工单号**（``INC-YYYYMMDD-NNNN``，由
    ``SequenceStore.next_ticket_number`` 生成），不是 agentflow 内部的 ticket id。
    名字沿用调用方契约；反查时两个都认（见 ``RecordStore.find_by_ticket``）。
    """

    ticket_id: str
    status: Literal["resolved", "failed", "insufficient"]
    description: str = ""


def _same_ticket_status(entry: dict, incoming: dict) -> bool:
    """两条回传条目是不是同一次。

    比对时**必须排除时间戳**——``reported_at`` 每次都不同，带上它这条判据永远为假，
    幂等就形同虚设（而症状恰好是"看不出问题"：重放时多一条证据，界面照常渲染）。
    """
    return (
        isinstance(entry, dict)
        and entry.get("type") == "ticket_status"
        and entry.get("ticket_id") == incoming["ticket_id"]
        and entry.get("status") == incoming["status"]
        and entry.get("description") == incoming["description"]
    )


@router.post("/ticket-status")
async def report_ticket_status(request: Request, body: TicketStatusBody) -> dict:
    """agentflow 修复工作流把工单状态回传到这里。

    按 ``ticket_id`` **反查**持有该工单的问题单（回传方手里只有工单号，没有
    ``record_id``）—— 查不到就 404，不静默丢。

    **人的裁定优先**：问题单若已被人工置为 ``resolved`` / ``closed``，回传只追加证据、
    不改状态。反过来的话，一次迟到的回传会**覆盖掉人刚做的判断**。
    """
    tenant = get_tenant_id(request)
    storage = request.app.state.storage
    ticket = (body.ticket_id or "").strip()
    if not ticket:
        raise AppException(ErrorCode.VALIDATION, "ticket_id 不能为空")
    # `status` 不用在这里再校验一次：本体是 `Literal`，pydantic 已经拦在入口
    # （422）。再加一段同样的判断就是**到不了的死代码**。

    rec = await storage.records.find_by_ticket(tenant, ticket)
    if rec is None:
        # 不建单、不猜：回传的是一个本租户没派出去过的号（或租户头错了）。
        raise AppException(ErrorCode.NOT_FOUND, f"没有持有工单 {ticket} 的问题单")
    record_id = rec["record_id"]

    entry = {
        "type": "ticket_status",
        "ticket_id": ticket,
        "status": body.status,
        "description": body.description,
        "reported_at": datetime.now(timezone.utc),
    }
    if any(_same_ticket_status(e, entry) for e in (rec.get("evidence") or [])):
        return {
            "ok": True, "duplicate": True, "record_id": record_id,
            "state": rec["state"], "state_changed": False,
        }

    await storage.records.append_evidence(tenant, record_id, entry)

    state_changed = False
    if body.status == _TICKET_RESOLVED and rec["state"] == "escalated":
        await storage.records.resolve(
            tenant, record_id, reason=f"agentflow:resolved:{ticket}"
        )
        state_changed = True
        note = "修复已落地，问题单置 resolved"
    elif body.status == _TICKET_RESOLVED:
        # 已经是 resolved/closed 等终态 → 人已经定过，不回写
        note = f"问题单已是 {rec['state']}，仅追加证据、不改状态（人的裁定优先于回传）"
    else:
        note = "修复未完成，问题单保持 escalated 等人处理"

    return {
        "ok": True,
        "duplicate": False,
        "record_id": record_id,
        "ticket_id": ticket,
        "status": body.status,
        "state": "resolved" if state_changed else rec["state"],
        "state_changed": state_changed,
        "note": note,
    }


# ── Analyze：把 problem_record 映射成平铺 ServiceNow 风格 ticket ──────────────

def _ticket_title(rec: dict) -> str:
    """短标题：优先 symptom.summary；空则取首条 metric / log 签名兜底。"""
    sym = (rec.get("symptom") or {}).get("summary") or ""
    if sym:
        return sym[:200]
    metric = rec.get("metric_anomalies") or []
    if metric:
        m = metric[0]
        return f"{rec.get('service', '?')} {m.get('metric', 'metric')} = {m.get('value', '?')}"[:200]
    logs = rec.get("log_anomalies") or []
    if logs:
        l0 = logs[0]
        return (l0.get("signature") or l0.get("pattern") or "log anomaly")[:200]
    return f"{rec.get('service', 'service')} anomaly"


def _ticket_description(rec: dict) -> str:
    """多行详情：元信息头 + summary + 各 metric/log 异常明细。"""
    lines = [
        f"service={rec.get('service')}",
        f"domain={rec.get('domain')}",
    ]
    if rec.get("instance"):
        lines.append(f"instance={rec['instance']}")
    lines.append(f"severity={rec.get('severity')}")
    if rec.get("detected_at") is not None:
        lines.append(f"detected_at={rec['detected_at']}")
    lines.append(f"occurrence_count={rec.get('occurrence_count', 1)}")
    if rec.get("trace_id"):
        lines.append(f"trace_id={rec['trace_id']}")
    sym = (rec.get("symptom") or {}).get("summary")
    if sym:
        lines.append(f"summary={sym}")
    for m in rec.get("metric_anomalies") or []:
        base = f" (baseline={m.get('baseline')})" if m.get("baseline") is not None else ""
        lines.append(f"metric {m.get('metric')}={m.get('value')}{base}")
    for lg in rec.get("log_anomalies") or []:
        lines.append(f"log[{lg.get('level')}] {lg.get('signature')} x{lg.get('count')}")
    return "\n".join(lines) or "no details"


def _first_log_chain_id(rec: dict) -> str | None:
    """取 evidence.log_trace_ids.trace_ids 第一个业务链路 ID 作 ticket.requestId。

    app 日志链路 API 按 ``requestId``（32 位业务链路 ID）查整条调用链
    （``/chain/{requestId}``）。rec.trace_id 是检测轮次 id（round_id，形如 ``trace-…``），
    二者不同源，故 requestId 必须取自 log 异常关联的业务 trace_ids（emit 时写入 evidence）。
    """
    for e in rec.get("evidence") or []:
        if not isinstance(e, dict):
            continue
        if e.get("type") == "log_trace_ids":
            tids = e.get("trace_ids") or []
            if tids:
                return str(tids[0])
    return None


def _build_ticket(rec: dict) -> dict:
    """problem_record → 平铺 ticket（全字段字符串化，确定性、按 severity 映射）。"""
    impact, urgency, priority = _SEV_MAP.get(
        (rec.get("severity") or "warning").lower(), ("3", "3", "3")
    )
    # cmdb_ci.name 是单值字段：跨服务记录取主服务（拼接串塞进去 CI 匹配不到）
    cmdb_ci: dict = {"name": _primary_service(rec)}
    if rec.get("domain"):
        cmdb_ci["service"] = rec["domain"]
    if rec.get("instance"):
        cmdb_ci["namespace"] = rec["instance"]
    ticket = {
        "number": rec["record_id"],
        "short_description": _ticket_title(rec),
        "description": _ticket_description(rec),
        "category": rec.get("domain") or "application",
        "subcategory": rec.get("source") or "apm-alert",
        "impact": impact,
        "urgency": urgency,
        "priority": priority,
        "state": "New",
        "cmdb_ci": cmdb_ci,
        "symptom": rec.get("symptom") or {"summary": ""},
        # 严重度：建单搬进 run 后（2026-09-21），agentflow 的建单节点只能从 run 的 inputs
        # 里拿它——工单列表要显示这一列，而 impact/urgency/priority 是**有损映射**
        # （3 档 → 3 档但语义不同），反推回去等于在 agentflow 侧复制一份 `_SEV_MAP`。
        "severity": rec.get("severity"),
    }
    req_id = _first_log_chain_id(rec)
    if req_id:
        # git-search 工作流入参：app-log-analyst 按它查链路日志（无则 require 预检判失败）
        ticket["requestId"] = req_id
    return ticket


# 时间窗推导参数（见 _analysis_window）。集中成常量是为了让"为什么是这几个数"一眼可见。
_WINDOW_PAD = timedelta(minutes=10)     # 前后各留一段，避免边界上的日志被切掉
_WINDOW_MIN = timedelta(minutes=30)     # 过窄的窗口取不到样本 → 撑到 30min
_WINDOW_MAX = timedelta(hours=24)       # MCP 侧 DATASOURCE_MAX_RANGE_HOURS 硬拒绝 >24h


def _as_utc(value) -> datetime | None:
    """记录里的时间字段 → aware UTC；不可解析返回 None。

    ⚠️ PG 的 ``TIMESTAMP(3)`` 读回来是 **naive**（无时区），这里一律按 UTC 解释 ——
    与 MCP 侧 ``_parse_window`` 的处理一致，两边才能在同一个窗口上对齐。
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _analysis_window(rec: dict, *, now: datetime | None = None) -> tuple[str, str]:
    """问题单 → 日志查询窗口（UTC ISO8601，落在 MCP 的硬约束内）。

    - 起：``first_seen_at``（缺则 ``detected_at``）− 10min
    - 止：``last_seen_at``（缺则 ``detected_at``）+ 10min，且**夹到 now**
      （检测轮的 ``last_seen_at`` 常晚于最新日志，不夹会去查未来）
    - 跨度：过窄（<30min）撑到 30min，过长（>24h）截到 24h
    """
    now = now or datetime.now(timezone.utc)
    detected = _as_utc(rec.get("detected_at")) or now
    start = (_as_utc(rec.get("first_seen_at")) or detected) - _WINDOW_PAD
    end = (_as_utc(rec.get("last_seen_at")) or detected) + _WINDOW_PAD
    if end > now:
        end = now
    if start > end:
        start = end - _WINDOW_MIN
    if end - start < _WINDOW_MIN:
        start = end - _WINDOW_MIN
    if end - start > _WINDOW_MAX:
        start = end - _WINDOW_MAX
    return start.isoformat(), end.isoformat()


def _agent_run_evidence(run_id: str, workflow_id: str) -> dict:
    """``agent_run`` evidence 条目（与 ``records.mark_in_progress`` 写的形状一致）。"""
    return {
        "type": "agent_run",
        "run_id": run_id,
        "workflow_id": workflow_id,
        "started_at": datetime.now(timezone.utc),
    }


def _build_analysis_inputs(rec: dict) -> dict:
    """问题单 → agentflow workflow 的 ``inputs``。

    ⚠️ 必须**包一层** ``bug_report``：``_build_ticket`` 的平铺形状就是"工单/事件"对象本身，
    而 workflow 里各 agent 的入参是 ``$.inputs.bug_report[.cmdb_ci.name]``。早期直接把平铺
    ticket 当 inputs 发出去，所有 ``$.inputs.bug_report`` 都解析成 None —— 工作流在
    ``triage.require: [bug]`` 处就 ``NodeInputError`` 失败，页面上看不出真因。

    ``review_feedback``：上一次驳回时人写的修改建议，**恒存在**（无驳回时为空串），让 workflow
    侧不必区分"键缺失"与"值为空"两种情况。它是"带建议重跑"成真的载体 —— 在此之前这段文字
    只落进 evidence，**从未进入新一轮 run 的输入**，于是 UI 那句"带着这条建议重新分析"是空头
    支票，重跑的输入与上一轮逐字节相同。
    """
    start, end = _analysis_window(rec)
    return {
        "bug_report": _build_ticket(rec),
        "window_start": start,
        "window_end": end,
        "review_feedback": _latest_reject_feedback(rec),
    }


@router.post("/{record_id}/analyze")
async def analyze_problem(request: Request, record_id: str, body: AnalyzeProblemBody) -> dict:
    """对问题单发起 agentflow 工作流分析（Problem Center「分析new」与「Analyze」共用）。

    顺序：1) 校验 workflow_id → 2) 记录存在性（404）→ 3) 终态守卫（resolved/closed/archived → 409）→
    4) state=pending 正常开跑；state=in_progress 且 ``rerun=true`` → 起新 run 并追加绑定
    （打回后重跑），否则 409 → 5) 组 ``inputs``（``bug_report`` 包装 + 时间窗）→
    6) POST agentflow ``/run``（失败/非 2xx → 502，绝不翻转状态）→ 7) 成功才写绑定/翻状态。

    ⚠️ 第 6 步的超时**不等于失败**：agentflow 在响应前同步准备工作区，超时只表示我们不等了，
    它那边照样把 run 建起来并跑。故超时用独立文案（见 ``_RUN_START_TIMEOUT_REASON``），
    且超时后**不写绑定**——这正是"点了报错、后台其实在跑、重试又多一个 run"的来源。
    超时值走 ``settings.run_start_timeout_sec``（专用，不共用采集器的 ``outbound_timeout_sec``）。
    """
    workflow_id = (body.workflow_id or "").strip()
    if not workflow_id:
        raise AppException(ErrorCode.VALIDATION, "workflow_id is required")

    tenant = get_tenant_id(request)
    storage = request.app.state.storage
    settings = request.app.state.settings
    rec = await storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")
    state = rec.get("state") or "pending"
    if state in _TERMINAL_STATES:
        raise AppException(
            ErrorCode.CONFLICT,
            f"problem {record_id} is {state}; analysis not allowed",
        )
    if state != "pending" and not body.rerun:
        raise AppException(
            ErrorCode.CONFLICT,
            f"problem {record_id} is not pending (state={state})",
        )
    if not _primary_service(rec):
        # 服务名是取数的查询目标（日志/仓库都按它过滤）：没有它下游只能靠猜，宁可在入口拒绝。
        raise AppException(ErrorCode.VALIDATION, "no service available for analysis")

    inputs = _build_analysis_inputs(rec)
    base = str(settings.bug_solve_base_url).rstrip("/")
    url = base + "/run"
    # 出站安全网关：base 为 operator 配置地址；回环仅当 APM_ALLOW_LOOPBACK=true 放行（本地 .env 已设）。
    OutboundGateway.validate_url(url)
    http = request.app.state.http_client
    # 两侧租户不同（问题单在本仓租户，workflow/MCP 在 agentflow 租户）→ 显式桥接，见 settings.agentflow_tenant。
    # ⚠️ **头与 body 都要给**：`POST /run` 的租户在 dev 模式取自 body 的 `tenant_id`
    #   （`RunRequest.tenant_id` 缺省 "local"），而 workflow 查找走 X-Tenant-ID 头。
    #   只给头 → 流程从 A 租户读、run 却落在 agentflow 的 local 租户（且那里没有 MCP 绑定，
    #   agent 没有工具 → 节点失败）。这个"半程换租户"没有任何提示，实测踩过。
    agentflow_tenant = settings.agentflow_tenant or tenant
    headers = {"Content-Type": "application/json", "X-Tenant-ID": agentflow_tenant}
    try:
        resp = await http.request(
            "POST",
            url,
            json={"workflow_id": workflow_id, "ticket": inputs, "tenant_id": agentflow_tenant},
            headers=headers,
            timeout=settings.run_start_timeout_sec,
        )
    except httpx.TimeoutException as exc:
        # 超时 ≠ 没起来：agentflow 的 POST /run 在返回前**同步**准备工作区（拉修复侧仓库），
        # 耗时可能超过任何合理超时。超时只表示**我们不等了**，它那边照建照跑。
        # 所以措辞必须拦住盲目重试 —— 重试会再起一个 run（每个还占租户并发配额）。
        raise AppException(
            ErrorCode.UPSTREAM, _RUN_START_TIMEOUT_REASON.format(secs=settings.run_start_timeout_sec)
        ) from exc
    except httpx.HTTPError as exc:  # 连接 / 超时
        raise AppException(ErrorCode.UPSTREAM, f"agent workflow run start failed: {exc}") from exc

    if resp.status_code >= 300:
        detail = ""
        try:
            payload = resp.json()
            detail = str(payload.get("detail") or payload.get("reason") or "")
        except Exception:  # noqa: BLE001 -- 兜底取原文前 200 字符
            detail = (resp.text or "")[:200]
        raise AppException(
            ErrorCode.UPSTREAM,
            f"agent workflow run start failed: HTTP {resp.status_code} {detail}",
        )

    try:
        run_id = str((resp.json() or {}).get("run_id") or "")
    except Exception as exc:  # noqa: BLE001
        raise AppException(ErrorCode.UPSTREAM, "agent workflow run start returned invalid body") from exc
    if not run_id:
        raise AppException(ErrorCode.UPSTREAM, "agent workflow run start returned no run_id")

    if state == "pending":
        flipped = await storage.records.mark_in_progress(
            tenant, record_id, run_id=run_id, workflow_id=workflow_id
        )
        if not flipped:
            # 并发场景：run 已启动，但记录已被移出 pending（如他处 resolve）
            raise AppException(
                ErrorCode.CONFLICT, f"problem {record_id} was concurrently moved out of pending"
            )
    elif not await storage.records.append_evidence(
        tenant, record_id, _agent_run_evidence(run_id, workflow_id)
    ):
        # 重跑路径：记录在被删/租户不符时 append 返回 False
        raise AppException(ErrorCode.CONFLICT, f"problem {record_id} vanished before binding")

    # 走到这里 state 必为 in_progress：pending 刚被翻转，rerun 路径本来就是 in_progress
    return {"record_id": record_id, "state": "in_progress", "run_id": run_id}


# ── 审批结果回写：agentflow run 的人工审批结论落 evidence（**只记录，不执行修复**）──────
#
# 审批本身发生在 agentflow 侧（``POST /runs/{id}/approve|reject``），本仓不参与决策；
# 这里只把结论记一笔，让问题单上有痕迹可查——否则审批完问题单看不出任何变化，
# 而唯一的关单动作（Ignore）写的是"误报"，语义不对。
# 按 ``(run_id, node_id)`` 幂等：前端重试/重复点击不会写第二条。


class RunDecisionBody(BaseModel):
    run_id: str
    node_id: str = "approve-plan"
    approved: bool
    by: str = ""
    comment: str = Field("", max_length=2000)


def _run_decisions(rec: dict) -> list[dict]:
    """记录里所有 ``run_decision`` evidence（审批痕迹）。"""
    return [
        e
        for e in rec.get("evidence") or []
        if isinstance(e, dict) and e.get("type") == "run_decision"
    ]


@router.post("/{record_id}/run-decision")
async def record_run_decision(request: Request, record_id: str, body: RunDecisionBody) -> dict:
    """记录 agentflow run 的审批结论（幂等；不改变问题单状态）。"""
    tenant = get_tenant_id(request)
    storage = request.app.state.storage
    rec = await storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")

    run_id = (body.run_id or "").strip()
    if not run_id:
        raise AppException(ErrorCode.VALIDATION, "run_id is required")
    node_id = (body.node_id or "").strip() or "approve-plan"

    for e in _run_decisions(rec):
        if e.get("run_id") == run_id and e.get("node_id") == node_id:
            return {
                "record_id": record_id,
                "run_id": run_id,
                "node_id": node_id,
                "approved": bool(e.get("approved")),
                "recorded": False,
                "duplicate": True,
            }

    entry = {
        "type": "run_decision",
        "run_id": run_id,
        "node_id": node_id,
        "approved": bool(body.approved),
        "by": body.by,
        "comment": body.comment,
        "decided_at": datetime.now(timezone.utc),
    }
    if not await storage.records.append_evidence(tenant, record_id, entry):
        raise AppException(ErrorCode.CONFLICT, f"problem {record_id} vanished before recording")
    return {
        "record_id": record_id,
        "run_id": run_id,
        "node_id": node_id,
        "approved": entry["approved"],
        "recorded": True,
        "duplicate": False,
    }


# ── 分析new：问题单 → 拼装诊断服务 /diagnose/logs，session_id 绑回 evidence ──────
#
# 与上面的 Analyze（→ agentflow workflow run）并存、互不影响：本路径不翻 state，
# 只在 evidence 追加一条 ``diagnose_session``，前端据此把按钮置灰 + 展示诊断内容。


class DiagnoseProblemBody(BaseModel):
    """可选的字段覆盖；缺省全部从 problem_record 推导（后端拼装）。"""

    app: str | None = None
    repo: str | None = None
    log_excerpt: str | None = None
    trace_id: str | None = None
    environment: str | None = None
    time_window: str | None = None


def _first_log_excerpt(rec: dict) -> str:
    """日志摘录：首条 log 异常的 signature（无则 pattern），再退 symptom.summary。"""
    for lg in rec.get("log_anomalies") or []:
        excerpt = lg.get("signature") or lg.get("pattern")
        if excerpt:
            return str(excerpt)
    return str((rec.get("symptom") or {}).get("summary") or "")


def _build_diagnose_body(rec: dict, body: DiagnoseProblemBody, settings) -> dict:
    """problem_record → ``/diagnose/logs`` 请求体（app/repo/trace_id/log_excerpt）。

    - app：``record.service`` 的**主服务**（服务身份即日志源）。跨服务记录（M9）的 service
      是拼接串，直接下发会让诊断服务找不到应用，故取第一个；
    - repo：请求覆盖 > ``settings.diagnose_repo`` > 主服务（spike 侧 repo 即仓库定位）；
    - trace_id：请求覆盖 > evidence 里首条业务链路 ID（``_first_log_chain_id``）；
    - log_excerpt：请求覆盖 > 首条日志异常签名。
    """
    payload: dict = {
        "app": body.app or _primary_service(rec),
        "repo": body.repo or settings.diagnose_repo or _primary_service(rec),
    }
    trace_id = body.trace_id or _first_log_chain_id(rec)
    if trace_id:
        payload["trace_id"] = trace_id
    if body.environment:
        payload["environment"] = body.environment
    if body.time_window:
        payload["time_window"] = body.time_window
    payload["log_excerpt"] = (body.log_excerpt or _first_log_excerpt(rec)).strip()
    return payload


def _latest_diagnose_session(rec: dict) -> dict | None:
    """取记录里最新一条 ``diagnose_session`` evidence（session_id → 诊断内容的桥）。"""
    latest = None
    for e in rec.get("evidence") or []:
        if isinstance(e, dict) and e.get("type") == "diagnose_session" and e.get("session_id"):
            latest = e
    return latest


def _latest_reject_feedback(rec: dict) -> str:
    """最近一次「驳回」时人写的修改建议（``feedback``）；没有则空串。

    只认 ``decision == "reject"``：后端对驳回**强制**要求带建议（见 ``_decide_agentflow`` 的
    "拒绝必须带修改建议"），其余裁定（ignore / false_positive / escalate）本来就没有这段文字——
    手工升级那条 evidence 里**连 ``feedback`` 键都没有**，故一律 ``.get``。
    """
    latest = ""
    for e in rec.get("evidence") or []:
        if (
            isinstance(e, dict)
            and e.get("type") == "diagnose_decision"
            and e.get("decision") == "reject"
            and (e.get("feedback") or "").strip()
        ):
            latest = e["feedback"].strip()
    return latest


def _agent_run_entries(rec: dict) -> list[dict]:
    """记录里所有 ``agent_run`` evidence，**按写入顺序**（重跑追加在尾部）。

    这就是「历次执行」的清单：每一轮 ``分析new``/``Analyze`` 都会追加一条，
    尾部那条即当前轮。与 :func:`_latest_diagnose_session` 同源思路，只是要全量而非最新一条。
    """
    return [
        e
        for e in rec.get("evidence") or []
        if isinstance(e, dict) and e.get("type") == "agent_run" and e.get("run_id")
    ]


def _latest_agent_run(rec: dict) -> dict | None:
    """最新一轮 agentflow run（``agent_run`` evidence 的尾部）。"""
    entries = _agent_run_entries(rec)
    return entries[-1] if entries else None


@router.post("/{record_id}/diagnose")
async def diagnose_problem(
    request: Request, record_id: str, body: DiagnoseProblemBody | None = None
) -> dict:
    """按问题单拼装 ``POST {diagnose_base_url}/diagnose/logs``，把 session_id 绑回 evidence。

    顺序：1) 记录存在性（404）→ 2) 终态守卫（resolved/closed → 409）→ 3) 已绑诊断（409，
    按钮已置灰，防重复发起）→ 4) 拼装请求体（缺 log_excerpt → 400）→ 5) 出站（连接/超时/
    非 2xx → 502，绝不落绑定）→ 6) 成功才 ``append_evidence`` 绑定 session_id。
    """
    body = body or DiagnoseProblemBody()
    tenant = get_tenant_id(request)
    storage = request.app.state.storage
    settings = request.app.state.settings
    rec = await storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")
    if (rec.get("state") or "pending") in _TERMINAL_STATES:
        raise AppException(
            ErrorCode.CONFLICT,
            f"problem {record_id} is {rec.get('state')}; diagnosis not allowed",
        )
    if _latest_diagnose_session(rec) is not None:
        raise AppException(ErrorCode.CONFLICT, f"problem {record_id} already has a diagnosis")

    payload = _build_diagnose_body(rec, body, settings)
    if not payload.get("log_excerpt"):
        raise AppException(ErrorCode.VALIDATION, "no log excerpt available for diagnosis")
    if not payload.get("app"):
        raise AppException(ErrorCode.VALIDATION, "no app available for diagnosis")

    base = str(settings.diagnose_base_url).rstrip("/")
    url = base + "/diagnose/logs"
    # 出站安全网关：base 为 operator 配置地址；回环仅当 APM_ALLOW_LOOPBACK=true 放行（本地 .env 已设）。
    OutboundGateway.validate_url(url)
    http = request.app.state.http_client
    try:
        resp = await http.request("POST", url, json=payload)
    except httpx.HTTPError as exc:  # 连接 / 超时
        raise AppException(ErrorCode.UPSTREAM, f"diagnosis start failed: {exc}") from exc

    if resp.status_code >= 300:
        detail = ""
        try:
            data = resp.json()
            detail = str(data.get("detail") or data.get("reason") or "")
        except Exception:  # noqa: BLE001 -- 兜底取原文前 200 字符
            detail = (resp.text or "")[:200]
        raise AppException(
            ErrorCode.UPSTREAM,
            f"diagnosis start failed: HTTP {resp.status_code} {detail}",
        )

    try:
        session_id = str((resp.json() or {}).get("session_id") or "")
    except Exception as exc:  # noqa: BLE001
        raise AppException(ErrorCode.UPSTREAM, "diagnosis start returned invalid body") from exc
    if not session_id:
        raise AppException(ErrorCode.UPSTREAM, "diagnosis start returned no session_id")

    entry = {
        "type": "diagnose_session",
        "session_id": session_id,
        "status": "analyzing",
        "app": payload.get("app"),
        "repo": payload.get("repo"),
        "trace_id": payload.get("trace_id"),
        "started_at": datetime.now(timezone.utc),
    }
    if not await storage.records.append_evidence(tenant, record_id, entry):
        # 并发场景：诊断已发起，但记录被删/租户不符
        raise AppException(ErrorCode.CONFLICT, f"problem {record_id} vanished before binding")

    return {"record_id": record_id, "session_id": session_id, "status": "analyzing"}


@router.get("/{record_id}/diagnose")
async def get_problem_diagnosis(request: Request, record_id: str) -> dict:
    """读该问题单当前诊断的**归一视图模型**（见 :mod:`aiops_apm.diagnosis`）。

    按记录 evidence 里的绑定分派引擎：

    - 有 ``agent_run`` → agentflow run 适配（``problem-log-diagnose`` 工作流，新路径）
    - 有 ``diagnose_session`` → spike ``/status/{session_id}`` 适配（老记录路径）
    - 两者都无 → 404

    两条路径返回**同一形状**，UI 不需要分辨引擎。响应额外带 ``executions``——
    该问题单的历次执行（每轮 run 一条，含实时状态），供弹窗底部的执行清单使用。

    读路径不因上游抖动整屏失败：agentflow 拉不到时降级为带 ``error`` 的骨架视图，
    而不是 502——否则用户连"这一轮还在跑"都看不到。
    """
    tenant = get_tenant_id(request)
    storage = request.app.state.storage
    settings = request.app.state.settings
    rec = await storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")

    issue_title = _ticket_title(rec)
    executions = await from_agentflow.fetch_executions(request, _agent_run_entries(rec))

    run_entry = _latest_agent_run(rec)
    if run_entry is not None:
        run_id = str(run_entry["run_id"])
        run = await from_agentflow.fetch_run(request, run_id)
        if run is None:
            return from_agentflow.build_unavailable(issue_title=issue_title)
        traces = await from_agentflow.fetch_traces(request, run_id)
        return from_agentflow.build(
            run, traces=traces, executions=executions, issue_title=issue_title
        )

    entry = _latest_diagnose_session(rec)
    if entry is None:
        raise AppException(ErrorCode.NOT_FOUND, f"no diagnosis bound to problem {record_id}")
    session_id = str(entry["session_id"])

    url = str(settings.diagnose_base_url).rstrip("/") + "/status/" + session_id
    OutboundGateway.validate_url(url)
    http = request.app.state.http_client
    try:
        resp = await http.request("GET", url)
    except httpx.HTTPError as exc:
        raise AppException(ErrorCode.UPSTREAM, f"diagnosis status fetch failed: {exc}") from exc
    if resp.status_code >= 300:
        raise AppException(
            ErrorCode.UPSTREAM, f"diagnosis status fetch failed: HTTP {resp.status_code}"
        )
    try:
        snap = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise AppException(ErrorCode.UPSTREAM, "diagnosis status returned invalid body") from exc
    if not isinstance(snap, dict):
        raise AppException(ErrorCode.UPSTREAM, "diagnosis status returned invalid body")
    return from_spike.build(snap, executions=executions, issue_title=issue_title)


# ── 审批决策：拒绝重跑 / 忽略关单 / 误报关单（history 落本仓 evidence）─────────────
#
# 诊断会话（spike）的 ``remediation`` 是**单值**且每次重跑即清空 → 历史计划在 spike 侧必丢；
# 真源只能是这里的 ``problem_record.evidence``（``diagnose_decision`` 条目），故历史走独立只读端点，
# ``GET /{id}/diagnose`` 仍逐字回传 spike 快照（UI 直接读顶层字段），响应形状不被污染。
#
# 且不仅 ``remediation`` 会丢——重跑会把整轮现场推平（spike ``runner.py:117-121`` 清空
# ``raw_reply``/``tasks``/``tool_calls``/``conclusion``/``remediation``），
# 所以"第一轮所有信息"（根因/总结/全部方案/分析链路）只能靠**决策那一刻先抓一份 ``/status`` 快照**
# 落进 ``diagnose_decision.snapshot``（见 ``_fetch_snapshot`` / ``_bounded_snapshot``）。
# 三态（拒绝/忽略/误报）都抓；抓不到则降级（``snapshot=None``），UI 侧回落到 ``steps``。


class DiagnoseDecisionBody(BaseModel):
    """诊断输出的三种裁定。

    ``false_positive`` **保留但前端不再出按钮**（2026-09-21「升级」顶掉了它在处理模块上的位置）：
    它的后端链路（FPR 回写 + `resolve(reason=false_positive)`）仍被 M7 的既有测试与
    `POST /{id}/resolve{false_positive:true}` 使用，删枚举值是不必要的破坏性改动。
    """

    decision: Literal["reject", "ignore", "escalate", "false_positive"]
    feedback: str = Field("", max_length=2000)  # reject 必填非空；其余忽略之
    option_index: int | None = None  # 1-based；缺省 → spike 侧推荐方案


def _diagnose_decisions(rec: dict) -> list[dict]:
    """按写入顺序取记录里所有 ``diagnose_decision`` evidence（历史计划）。"""
    return [
        e
        for e in rec.get("evidence") or []
        if isinstance(e, dict) and e.get("type") == "diagnose_decision"
    ]


def _resp_detail(resp: httpx.Response) -> str:
    """从 spike 错误响应里提 ``detail``/``reason``，兜底取原文前 200 字符。"""
    try:
        data = resp.json()
        return str(data.get("detail") or data.get("reason") or "")
    except Exception:  # noqa: BLE001
        return (resp.text or "")[:200]


async def _spike_post(request: Request, path: str, payload: dict) -> httpx.Response:
    """向诊断服务 POST（出站安全网关 + 连接/超时映射 502）。"""
    settings = request.app.state.settings
    url = str(settings.diagnose_base_url).rstrip("/") + path
    OutboundGateway.validate_url(url)
    http = request.app.state.http_client
    try:
        return await http.request("POST", url, json=payload)
    except httpx.HTTPError as exc:  # 连接 / 超时
        raise AppException(ErrorCode.UPSTREAM, f"diagnosis service call failed: {exc}") from exc


async def _spike_get(request: Request, path: str) -> httpx.Response:
    """向诊断服务 GET（出站安全网关 + 连接/超时映射 502）。"""
    settings = request.app.state.settings
    url = str(settings.diagnose_base_url).rstrip("/") + path
    OutboundGateway.validate_url(url)
    http = request.app.state.http_client
    try:
        return await http.request("GET", url)
    except httpx.HTTPError as exc:  # 连接 / 超时
        raise AppException(ErrorCode.UPSTREAM, f"diagnosis service call failed: {exc}") from exc


async def _fetch_snapshot(request: Request, session_id: str) -> dict | None:
    """抓该会话 ``/status`` 快照做历史留存；**任何失败都返回 None，绝不阻断决策**。

    决策（拒绝/忽略/误报）带下游副作用（重跑 / 关单 / FPR 回写），抓快照只为留档——
    网络抖动、会话已过期（404）、上游 5xx 都不该让"关单"失败。故这里吞掉所有异常。
    """
    try:
        resp = await _spike_get(request, f"/status/{session_id}")
    except AppException:
        return None
    if resp.status_code >= 300:
        return None
    try:
        snap = resp.json()
    except Exception:  # noqa: BLE001
        return None
    return snap if isinstance(snap, dict) else None


_MAX_SUPPORTING_TEXT = 4000
_MAX_SNAPSHOT_BYTES = 128 * 1024


def _bounded_snapshot(snap: dict) -> dict:
    """给快照上体积护栏，保证单次病态长输出撑不爆 JSONB 行。

    - 恒做：``conclusion.evidence[].supporting_text`` 每项截到 4000 字符（唯一无界字段）；
    - 若序列化后仍 > 128KB：丢掉 ``tool_calls``（保 conclusion/tasks 等），
      分析链路的工具调用是最大头且可弃。
    """
    conclusion = snap.get("conclusion")
    if isinstance(conclusion, dict):
        evidence = conclusion.get("evidence")
        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                text = item.get("supporting_text")
                if isinstance(text, str) and len(text) > _MAX_SUPPORTING_TEXT:
                    item["supporting_text"] = text[:_MAX_SUPPORTING_TEXT]
    try:
        if len(json.dumps(snap, default=str)) > _MAX_SNAPSHOT_BYTES:
            snap.pop("tool_calls", None)
    except (TypeError, ValueError):
        pass
    return snap


async def _agentflow_reject_node(
    request: Request, tenant: str, run_id: str, node_id: str, *, by: str, comment: str
) -> str:
    """驳回 agentflow run 上的审批节点；返回节点状态描述（失败不抛，返回错误说明）。

    审批是**终态 CAS、不可逆**，且拒绝会走复盘的 ``recap`` 分支收尾——和 spike 侧的
    ``/approve{decision:reject}`` 语义对齐。这里对失败宽容：节点可能已被别处驳回
    （CAS 冲突 409），那不影响"关掉这条问题单"这个更重要的动作。
    """
    settings = request.app.state.settings
    url = str(settings.bug_solve_base_url).rstrip("/") + f"/runs/{run_id}/reject"
    OutboundGateway.validate_url(url)
    http = request.app.state.http_client
    agentflow_tenant = settings.agentflow_tenant or tenant
    try:
        resp = await http.request(
            "POST",
            url,
            json={"node_id": node_id, "by": by, "comment": comment},
            headers={"Content-Type": "application/json", "X-Tenant-ID": agentflow_tenant},
        )
    except httpx.HTTPError as exc:
        return f"unreachable: {exc}"
    if resp.status_code >= 300:
        return f"failed: HTTP {resp.status_code} {_resp_detail(resp)}"
    return "rejected"


async def _agentflow_approve_node(
    request: Request, tenant: str, run_id: str, node_id: str, *, by: str, comment: str = ""
) -> str:
    """放行 agentflow run 上的审批节点（「升级」用）；返回节点状态描述，**失败不抛**。

    与 :func:`_agentflow_reject_node` 逐字同构，差别只在 URL（``/approve``）与语义。
    失败宽容的理由也一样：审批是**终态 CAS、不可逆**——重试一次「升级」时节点早已被答复，
    二次 approve 必然 CAS 冲突（409），那不是错误，也不该挡住"建工单"这个更重要的动作。

    但**不答这个门后果更重**：run 会永远停在 ``waiting_approval``，而那是 agentflow 的
    活动状态、**一直占着租户的并发额度**，直到审批超时（本流程 86400s）才自愈。
    """
    settings = request.app.state.settings
    url = str(settings.bug_solve_base_url).rstrip("/") + f"/runs/{run_id}/approve"
    OutboundGateway.validate_url(url)
    http = request.app.state.http_client
    agentflow_tenant = settings.agentflow_tenant or tenant
    try:
        resp = await http.request(
            "POST",
            url,
            json={"node_id": node_id, "by": by, "comment": comment},
            headers={"Content-Type": "application/json", "X-Tenant-ID": agentflow_tenant},
        )
    except httpx.HTTPError as exc:
        return f"unreachable: {exc}"
    if resp.status_code >= 300:
        return f"failed: HTTP {resp.status_code} {_resp_detail(resp)}"
    return "approved"


def _agentflow_gate_node(run: dict | None) -> str | None:
    """该 run 当前**真正在等**的审批节点 id；**没有在等的门则返回 None**。

    为什么不能写死：审批节点的 id 住在 workflow 的 YAML 里，改名（``approve-plan`` →
    ``diagnose-output``）会让写死的那份**静默打偏**——reject/approve 打到一个不存在的
    节点，run 永远停在 ``waiting_approval`` 占着并发额度，而调用方看不出是节点名对不上。

    ⚠️ 原实现读 ``run_entry["approval_node_id"]``，而那个字段**全仓从来没有任何写入方**
    （只有那一处读），所以恒等于写死的回退值——是个只在改名时才暴露的哑弹。改成从 run 的
    ``pending_approvals`` 现取（形状见 ``GET /runs/{id}``）。

    ⚠️ **返回 None 时调用方必须不发那次出站调用**（不要退回一个猜的节点 id）：
    - run 的状态列可能是**陈旧的 `waiting_approval`**（checkpoint 才是"门开着没有"的真源，
      ``pending_approvals`` 由节点状态算出），实测库里就有一条这样的记录；
    - 猜一个 id 打过去，agentflow 的 ``DAGExecutor.approve`` 第一行 ``self.dag.nodes[nid]``
      对不存在的节点抛 ``KeyError`` → **HTTP 500**（"节点存在但不在等待"才是干净的 400，
      那个 KeyError 没进异常映射，是既有缺陷）。
    - 就算打中一个存在的节点，也只会拿到 400 —— 一次注定的失败出站，还得在 evidence 里
      记一句读不懂的 "HTTP 500"。
    """
    pending = (run or {}).get("pending_approvals") or []
    for entry in pending:
        node_id = (entry or {}).get("node_id") if isinstance(entry, dict) else None
        if node_id:
            return str(node_id)
    return None


def _created_ticket(rec: dict) -> dict | None:
    """记录里已有的**升级裁定**（幂等判据）；没有则 None。

    ⚠️ 判据是 ``diagnose_decision`` + ``decision == "escalate"`` + 非空 ``ticket_id``，
    **不是**一个独立的 ``ticket_created`` 条目——「升级」与「它建出的那张工单」是
    **同一件事**，拆成两条 evidence 会让"读的人得自己把两条拼起来"，也会多一个
    只写不读的类型。

    ``ticket_id`` 非空是必要的第二条件：escalate 分支在**建单失败**时不会走到写 evidence
    （那时直接抛 502），但历史里可能留着更早期的、没有工单号的 escalate 条目。
    """
    for e in rec.get("evidence") or []:
        if (
            isinstance(e, dict)
            and e.get("type") == "diagnose_decision"
            and e.get("decision") == "escalate"
            and e.get("ticket_id")
        ):
            return e
    return None


#: 回读建单结果的轮询参数（`_read_escalation_ticket`）。
#:
#: 为什么要有轮询：`inline`（默认）模式下 `POST /runs/{id}/approve` 会**同步跑完剩余
#: DAG**（`service.approve` 里那句 `await ex.run()`），返回时建单节点早就 DONE → 第一次
#: 读就有；而 `queue` 模式下 approve 只落 CAS + 发命令就返回，run 还在 Worker 手里。
#: 上限约 6s：节点本身只是一次 DB 插入，worker 的接单延迟占大头，正常远小于它。
_TICKET_POLL_ATTEMPTS = 15
_TICKET_POLL_INTERVAL_SEC = 0.4

#: `GET /runs/{id}` 的终态取值（agentflow 已把内部的 done 翻成 success）
_RUN_TERMINAL = ("success", "failed", "cancelled")


def _ticket_node_id(run: dict) -> str | None:
    """run 图里 `kind == "ticket"` 的节点 id。

    **按 kind 找，不写死节点名**——节点 id 住在 workflow 的 YAML 里，改名会让写死的那份
    **静默打偏**（今天刚在 `approve-plan → diagnose-output` 那次改名上踩过同一个坑）。
    """
    graph = run.get("graph")
    if not isinstance(graph, dict):
        return None
    for n in graph.get("nodes") or []:
        if isinstance(n, dict) and n.get("kind") == "ticket":
            return str(n.get("id") or "") or None
    return None


async def _read_escalation_ticket(request: Request, run_id: str) -> dict:
    """回读 run 里建单节点的输出（带**有界轮询**）。

    「升级」的建单动作住在 run 内部（`kind: ticket` 节点），本仓**只回读**、**不再自己建**
    ——两边都建会出两张单。

    拿不到就抛 `UPSTREAM` 且**不置终态**：重试是安全且自愈的（节点输出存在 checkpoint 里，
    第二次调用直接读到），所以宁可让人重试，也不要落一个说不清的终态。
    """
    for attempt in range(_TICKET_POLL_ATTEMPTS):
        run = await from_agentflow.fetch_run(request, run_id)
        if run is not None:
            node_id = _ticket_node_id(run)
            nodes = run.get("nodes")
            node = nodes.get(node_id) if (node_id and isinstance(nodes, dict)) else None
            node = node if isinstance(node, dict) else {}
            status = node.get("status")
            if status == "done":
                out = node.get("output")
                if isinstance(out, dict) and out.get("ticket_id"):
                    return out
                raise AppException(ErrorCode.UPSTREAM, "建单节点已完成但没有工单号")
            if status == "failed":
                raise AppException(
                    ErrorCode.UPSTREAM,
                    f"建单节点失败：{str(node.get('error') or '')[:200] or '见 run 详情'}",
                )
            if run.get("status") in _RUN_TERMINAL:
                raise AppException(
                    ErrorCode.UPSTREAM,
                    f"run 已 {run.get('status')}，但建单节点未执行（status={status}）"
                    "——门可能不是通过放行的",
                )
        if attempt < _TICKET_POLL_ATTEMPTS - 1:
            await asyncio.sleep(_TICKET_POLL_INTERVAL_SEC)
    raise AppException(ErrorCode.UPSTREAM, "工单生成中，请稍后重试")


async def _apply_escalated_state(storage, tenant: str, record_id: str, decision: str, recorded: dict) -> None:
    """``escalate`` 才把记录落成终态；非 escalate 是空操作。

    ⚠️ **必须在 ``append_evidence`` 之后调**——顺序反了（终态先落、evidence 没落）时，
    重试的幂等判据（``_created_ticket`` 读的是 evidence 里的升级裁定）不成立
    → **建出第二张工单**。
    这个顺序下最坏只是"单已建、状态还没翻"，重试会补齐（``mark_escalated`` 对同状态幂等）。

    ``reason`` 里带上工单号：它会显示在前端 Detail 弹窗的「Resolve Reason」行上，
    是**不展开 evidence 就能看到工单号**的唯一位置。
    """
    if decision != "escalate":
        return
    ticket_number = recorded.get("ticket_number") or ""
    await storage.records.mark_escalated(
        tenant, record_id, reason=f"escalated:{ticket_number}" if ticket_number else "escalated"
    )


async def _resolve_workflow_id(request: Request, name: str) -> str:
    """workflow **名字** → id（`POST /tickets` 只收 id，名字才是跨租户可移植的键）。

    名字在库里**可能不唯一**（`workflows.name` 没有唯一约束）：取 ``created_at`` 最新那条，
    与 agentflow 侧 ``get_by_name`` 同一口径 —— 否则"建单时钉的"与"发起时跑起来的"
    可能不是同一条，而两处都不会报错。

    查不到 → **404，绝不退回不钉**：钉 `bug-fix-scenario2` 却跑起一条别的流程，
    比跑不起来危险得多（这条与 agentflow `_workflow_for_ticket` 的 409 同一个理由）。
    """
    name = (name or "").strip()
    if not name:
        return ""
    items = await from_agentflow.fetch_workflows(request)
    if items is None:
        raise AppException(ErrorCode.UPSTREAM, "list workflows failed: agentflow 不可达")
    hits = [w for w in items if str(w.get("name") or "") == name and w.get("id")]
    if not hits:
        raise AppException(ErrorCode.NOT_FOUND, f"workflow「{name}」不在库里（被删或改名了）")
    hits.sort(key=lambda w: str(w.get("created_at") or ""), reverse=True)
    return str(hits[0]["id"])


async def _agentflow_create_ticket(
    request: Request, tenant: str, rec: dict, run: dict | None, *, workflow_id: str = ""
) -> dict:
    """在 agentflow 建一张修复工单，返回 ``{ticket_id, ticket_number}``。

    ⚠️ **只给 spike 老路径与「直接派单」用**（`_decide_agentflow` 的 escalate 分支**不再**调它）。
    agentflow 那条路径的建单已经搬进 run 内部（`kind: ticket` 节点），本仓改成回读——
    两边都建会出两张单。spike 会话与直接派单**没有 run**，也就没有那个节点可读，
    所以这两支保留自己的建单（老记录不至于点「升级」就撞死路）。

    ``workflow_id`` 非空 = 建单时就把这张单**钉**到该流程上（发起诊断时按它选，
    见 agentflow ``_workflow_for_ticket`` 的第二档）；空 = 不钉，沿用平台今天的默认。


    **失败抛 ``UPSTREAM``**——与上面两个 best-effort 的审批动作刻意不同：建单没成就不该
    对外说"已升级"，调用方据此**不置终态**，人可以重试。

    ``number`` 由本仓生成（``SequenceStore.next_ticket_number`` → ``INC-YYYYMMDD-NNNN``）：
    agentflow 的 ``tickets.number`` 列是**可空、无唯一约束、平台不生成**的自由文本列
    （它是"外部工单号"的语义），由派单方给号才对——这里 APM 正是派单方。

    ``bug_report`` 用 ``_build_ticket`` 原样 + 一份诊断结论摘要（``conclusion_digest``）：
    工单在 agentflow 的 Ticket Inbox 里可见，且 ``inputs`` 形态与 ``/analyze`` 一致，
    需要时可直接 ``POST /tickets/{tid}/run`` 把它跑起来。
    """
    settings = request.app.state.settings
    storage = request.app.state.storage
    agentflow_tenant = settings.agentflow_tenant or tenant

    bug_report = dict(_build_ticket(rec))
    digest = from_agentflow.conclusion_digest(run or {})
    if digest:
        bug_report["diagnosis"] = digest

    start, end = _analysis_window(rec)
    payload = {
        "title": _ticket_title(rec)[:200],
        "bug_report": bug_report,
        "number": await storage.sequence.next_ticket_number(),
        "service": _primary_service(rec),
        "namespace": rec.get("instance"),
        "severity": rec.get("severity"),
        "window_start": start,
        "window_end": end,
    }
    if workflow_id:
        # 钉流程：agentflow 收 id、把**名字**存进 tickets.workflow_name（见其 create_ticket）。
        payload["workflow_id"] = workflow_id

    url = str(settings.bug_solve_base_url).rstrip("/") + "/tickets"
    OutboundGateway.validate_url(url)
    # 租户头与 body 都带：agentflow 的 ``_ticket_inputs`` 从 body 的 bug_report 组装 inputs，
    # 而工单落哪张表由**头**决定（与 analyze_problem 同一条约定）。
    payload["tenant_id"] = agentflow_tenant
    http = request.app.state.http_client
    try:
        resp = await http.request(
            "POST",
            url,
            json=payload,
            headers={"Content-Type": "application/json", "X-Tenant-ID": agentflow_tenant},
        )
    except httpx.HTTPError as exc:
        raise AppException(ErrorCode.UPSTREAM, f"create ticket failed: {exc}") from exc
    if resp.status_code >= 300:
        raise AppException(
            ErrorCode.UPSTREAM,
            f"create ticket failed: HTTP {resp.status_code} {_resp_detail(resp)}",
        )
    try:
        row = resp.json() or {}
    except Exception as exc:  # noqa: BLE001
        raise AppException(ErrorCode.UPSTREAM, "create ticket returned invalid body") from exc
    ticket_id = str(row.get("id") or "")
    if not ticket_id:
        raise AppException(ErrorCode.UPSTREAM, "create ticket returned no id")
    return {"ticket_id": ticket_id, "ticket_number": str(row.get("number") or "")}


async def _decide_agentflow(
    request: Request,
    storage,
    tenant: str,
    rec: dict,
    record_id: str,
    run_entry: dict,
    body: DiagnoseDecisionBody,
) -> dict:
    """agentflow 路径的决策：驳回重跑 / 忽略关单 / 误报关单。

    与 spike 路径的差别在于"拒绝"要拆成两步——先把当前 run 的审批节点驳回（让它收尾），
    再起一轮新的 run（``rerun=true``）。引擎没有"重跑同一个 run"的语义，只能起新的一轮，
    新 run_id 追加进 ``evidence``，「历次执行」区据此列出每一轮。

    响应形状与 spike 路径**保持一致**（``session_id`` 承接 run_id），UI 不需要分支。
    """
    settings = request.app.state.settings
    run_id = str(run_entry["run_id"])
    workflow_id = str(run_entry.get("workflow_id") or "")

    # run 详情只拉一次，两处共用：门的 node_id（`_agentflow_gate_node`）与工单里的诊断摘要
    # （`conclusion_digest`）。取不到（None）两条路径都有兜底，不阻断决策。
    run = await from_agentflow.fetch_run(request, run_id)
    node_id = _agentflow_gate_node(run)

    # 驳回**一份方案**必须说明理由（前端也拦一层）。但 halt / 无结论时的重跑是**重试**语义 ——
    # 没有方案可驳，理由选填（留空 = 直接重试，适用于 code-locator 轮次耗尽这类执行类失败）。
    # 判据刻意取**服务端事实**（"这份诊断有没有给出方案"）而不是"客户端点了哪个按钮"：
    # 后者能被伪造，前者不能。复用 `conclusion_digest`（halt / 诊断失败时它返回 `{}`），
    # 它与页面上显示的结论**同源**，所以与前端 `canReject` 的判据天然一致。
    # 代价：这条校验挪到了取 run 之后，注定 400 的请求会多一次出站 GET —— 可接受。
    if body.decision == "reject" and not (body.feedback or "").strip():
        if from_agentflow.conclusion_digest(run or {}).get("recommended_fix"):
            raise AppException(ErrorCode.VALIDATION, "拒绝必须带修改建议")

    by = "problem-center"
    recorded: dict = {
        "type": "diagnose_decision",
        "decision": body.decision,
        "engine": "agentflow",
        "session_id": run_id,
        "option_index": body.option_index,
        "option_title": None,
        "steps": None,
        "feedback": body.feedback or "",
        "session_status": None,
        "remediation_status": None,
        "reanalyze_count": None,
        "max_reanalyze": None,
        "snapshot": None,
        "ticket_id": None,
        "ticket_number": None,
        "decided_at": datetime.now(timezone.utc),
    }

    if body.decision == "escalate":
        # 幂等前置：**看 evidence 不看 state**。重试路径真实存在——工单建出来但
        # append_evidence 失败时，人看到的是报错，而单已经在 agentflow 里了。
        existing = _created_ticket(rec)
        if existing is not None:
            recorded["remediation_status"] = "already_escalated"
        else:
            # 先放行门、再建单。反过来的话，"建单成功而 approve 失败"会留下一张
            # 已派单、run 却还挂在门上的记录——而挂在门上的 run 一直占着租户并发额度。
            # **没有在等的门就跳过这一步**（不猜一个 id 去打，见 `_agentflow_gate_node`）：
            # 那说明门早被答复过（run 的状态列可能是陈旧的 waiting_approval），
            # 或者这是个 halt/failed 的 run——两种情况下"派单"都不需要它。
            if node_id:
                recorded["remediation_status"] = await _agentflow_approve_node(
                    request, tenant, run_id, node_id, by=by
                )
            else:
                recorded["remediation_status"] = "no_pending_gate"
            # 建单**发生在 run 内**（`kind: ticket` 节点，2026-09-21 从本仓搬过去）——
            # 这里只**回读**它的输出。⚠️ **不要**在这里再调一次 `POST /tickets`：那会出两张单。
            # 缺 `source_ref` 的旧 run（建单节点还没上线时起的）会回读不到 → 报错可重试。
            existing = await _read_escalation_ticket(request, run_id)
        recorded["ticket_id"] = existing.get("ticket_id")
        recorded["ticket_number"] = existing.get("ticket_number")
        recorded["session_status"] = "escalated"
    elif body.decision == "reject":
        # 没有在等的门 → 跳过那次驳回（不猜 id；驳回只是让旧 run 收尾，起新一轮才是重点）。
        recorded["remediation_status"] = (
            await _agentflow_reject_node(
                request, tenant, run_id, node_id, by=by, comment=(body.feedback or "").strip()
            )
            if node_id
            else "no_pending_gate"
        )

        # 起新一轮：与 analyze_problem 同一条链路（起 run + 绑 evidence），
        # 复用其 inputs 组装与租户桥接约定。
        if not workflow_id:
            raise AppException(
                ErrorCode.CONFLICT,
                f"run {run_id} has no workflow_id bound; cannot rerun",
            )
        inputs = _build_analysis_inputs(rec)
        # ⚠️ 必须把**本轮**的反馈显式并进去：上面的 `rec` 是在追加本次裁定**之前**取的
        # （`decide_problem_diagnosis` 取记录 → 这里起 run → 收尾才 `append_evidence`），
        # 所以此刻 `rec["evidence"]` 里**还没有**人刚提交的这条 reject。只靠
        # `_latest_reject_feedback` 会让**第一次驳回的建议丢失**（要等第二次驳回才带上第一次的），
        # 而"带着建议重跑"恰恰就是它存在的理由。本轮值优先于 evidence 里的历史值。
        inputs["review_feedback"] = (body.feedback or "").strip() or inputs["review_feedback"]
        url = str(settings.bug_solve_base_url).rstrip("/") + "/run"
        OutboundGateway.validate_url(url)
        http = request.app.state.http_client
        agentflow_tenant = settings.agentflow_tenant or tenant
        try:
            resp = await http.request(
                "POST",
                url,
                json={"workflow_id": workflow_id, "ticket": inputs, "tenant_id": agentflow_tenant},
                headers={"Content-Type": "application/json", "X-Tenant-ID": agentflow_tenant},
                timeout=settings.run_start_timeout_sec,
            )
        except httpx.TimeoutException as exc:
            # 同 analyze_problem：超时不代表没起来，措辞要点明"可能已在跑"以拦住重试。
            raise AppException(
                ErrorCode.UPSTREAM, _RUN_START_TIMEOUT_REASON.format(secs=settings.run_start_timeout_sec)
            ) from exc
        except httpx.HTTPError as exc:
            raise AppException(
                ErrorCode.UPSTREAM, f"agent workflow rerun failed: {exc}"
            ) from exc
        if resp.status_code >= 300:
            raise AppException(
                ErrorCode.UPSTREAM,
                f"agent workflow rerun failed: HTTP {resp.status_code} {_resp_detail(resp)}",
            )
        try:
            new_run_id = str((resp.json() or {}).get("run_id") or "")
        except Exception as exc:  # noqa: BLE001
            raise AppException(
                ErrorCode.UPSTREAM, "agent workflow rerun returned invalid body"
            ) from exc
        if not new_run_id:
            raise AppException(ErrorCode.UPSTREAM, "agent workflow rerun returned no run_id")
        await storage.records.append_evidence(
            tenant, record_id, _agent_run_evidence(new_run_id, workflow_id)
        )
        # 记录 state 不变（仍在 in_progress，新一轮跑着）
        recorded["session_status"] = "running"
    else:
        # 忽略 / 误报：尽力驳回节点（让 run 收尾），然后关单。
        # 同样地，没有在等的门就跳过那次驳回——理由与 escalate 分支那句相同。
        recorded["remediation_status"] = (
            await _agentflow_reject_node(
                request, tenant, run_id, node_id, by=by, comment=body.decision
            )
            if node_id
            else "no_pending_gate"
        )
        recorded["session_status"] = "dismissed"
        if body.decision == "false_positive":
            await _record_fpr(storage, tenant, rec, false_positive=True)
            await storage.records.resolve(tenant, record_id, reason="false_positive")
        else:
            await storage.records.close(tenant, record_id, reason="ignored")

    if not await storage.records.append_evidence(tenant, record_id, recorded):
        raise AppException(ErrorCode.CONFLICT, f"problem {record_id} vanished before recording")

    await _apply_escalated_state(storage, tenant, record_id, body.decision, recorded)

    fresh = await storage.records.get(tenant, record_id)
    return {
        "record_id": record_id,
        "decision": body.decision,
        "session_id": run_id,
        "session_status": recorded["session_status"],
        "remediation_status": recorded["remediation_status"],
        "reanalyze_count": recorded["reanalyze_count"],
        "max_reanalyze": recorded["max_reanalyze"],
        "ticket_id": recorded["ticket_id"],
        "ticket_number": recorded["ticket_number"],
        "record_state": (fresh or rec).get("state"),
    }


@router.post("/{record_id}/diagnose/decision")
async def decide_problem_diagnosis(
    request: Request, record_id: str, body: DiagnoseDecisionBody
) -> dict:
    """对已完成的诊断下判断：拒绝（重跑）/ 忽略（关单）/ 误报（关单 + FPR 回写）。

    无论哪种决策，都先 best-effort 抓一份 ``/status`` 快照进 ``recorded["snapshot"]``
    （整轮历史：根因/总结/方案/分析链路）——因为接下来任一个动作都会让 spike 侧丢掉这一轮。

    - ``reject``：先 ``/remediate{option_index}`` 快照选中方案（→ 历史计划的唯一留存处），
      再 ``/approve{decision:"reject", feedback}`` 触发后台重跑；**记录 state 不变**。
      会话必须还活着（404 → 409"已过期，无法重跑"）。
    - ``ignore`` / ``false_positive``：``/dismiss`` 签终态；**404 容忍**（会话过期也要能关单，
      不制造 404 死胡同）；ignore → ``records.close(reason="ignored")``，
      false_positive → FPR 回写 + ``records.resolve(reason="false_positive")``。
    """
    tenant = get_tenant_id(request)
    storage = request.app.state.storage
    rec = await storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")
    if (rec.get("state") or "pending") in _TERMINAL_STATES:
        raise AppException(
            ErrorCode.CONFLICT,
            f"problem {record_id} is {rec.get('state')}; decision not allowed",
        )

    # 分派引擎：新路径（agentflow run）与老路径（spike 会话）形态不同，各自处理。
    # 对 UI 而言这是**同一个**决策入口，响应形状也保持一致（见下）。
    run_entry = _latest_agent_run(rec)
    if run_entry is not None:
        return await _decide_agentflow(
            request, storage, tenant, rec, record_id, run_entry, body
        )

    entry = _latest_diagnose_session(rec)
    if entry is None:
        raise AppException(ErrorCode.CONFLICT, f"no diagnosis bound to problem {record_id}")
    sid = str(entry["session_id"])

    recorded: dict = {
        "type": "diagnose_decision",
        "decision": body.decision,
        "session_id": sid,
        "option_index": None,
        "option_title": None,
        "steps": None,
        "feedback": body.feedback or "",
        "session_status": None,
        "remediation_status": None,
        "reanalyze_count": None,
        "max_reanalyze": None,
        "snapshot": None,
        "ticket_id": None,
        "ticket_number": None,
        "decided_at": datetime.now(timezone.utc),
    }

    # 入参先校验——拒绝必须带修改建议。放在抓快照之前，避免为注定 400 的请求白白出站一次。
    if body.decision == "reject" and not (body.feedback or "").strip():
        raise AppException(ErrorCode.VALIDATION, "拒绝必须带修改建议")

    # 先抓快照再动手：拒绝会立刻后台重跑并推平整轮现场（spike runner.py:117-121），
    # 事后取不到第一轮。best-effort——抓不到就降级（snapshot=None），不阻断决策。
    snap = await _fetch_snapshot(request, sid)
    if snap is not None:
        recorded["snapshot"] = _bounded_snapshot(snap)

    if body.decision == "reject":
        feedback = (body.feedback or "").strip()

        # 1) 选方案（快照 steps → 历史计划）
        remediate_payload: dict = {}
        if body.option_index is not None:
            remediate_payload["option_index"] = body.option_index
        resp = await _spike_post(request, f"/remediate/{sid}", remediate_payload)
        if resp.status_code == 404:
            raise AppException(ErrorCode.CONFLICT, "诊断会话已过期，无法重跑")
        if resp.status_code >= 300:
            raise AppException(
                ErrorCode.UPSTREAM,
                f"diagnosis remediate failed: HTTP {resp.status_code} {_resp_detail(resp)}",
            )
        try:
            rem = resp.json() or {}
        except Exception as exc:  # noqa: BLE001
            raise AppException(ErrorCode.UPSTREAM, "diagnosis remediate returned invalid body") from exc
        recorded["option_index"] = rem.get("option_index")
        recorded["option_title"] = rem.get("option_title")
        recorded["steps"] = rem.get("steps")

        # 2) 驳回 → 后台重跑（达上限则 closed_manual）
        resp2 = await _spike_post(
            request, f"/approve/{sid}", {"decision": "reject", "feedback": feedback}
        )
        if resp2.status_code >= 300:
            raise AppException(
                ErrorCode.UPSTREAM,
                f"diagnosis reject failed: HTTP {resp2.status_code} {_resp_detail(resp2)}",
            )
        try:
            appr = resp2.json() or {}
        except Exception as exc:  # noqa: BLE001
            raise AppException(ErrorCode.UPSTREAM, "diagnosis reject returned invalid body") from exc
        recorded.update(
            session_status=appr.get("session_status"),
            remediation_status=appr.get("remediation_status"),
            reanalyze_count=appr.get("reanalyze_count"),
            max_reanalyze=appr.get("max_reanalyze"),
        )
        # 记录 state 不变（仍在 pending/in_progress，重跑中）
    elif body.decision == "escalate":
        # 老路径（spike 会话）没有 agentflow 审批门，「放行门」这一步自然省掉；
        # 其余与 agentflow 路径同构——同一份幂等判据、同一个建单 helper、同样的落库顺序。
        # ``run=None``：spike 会话读不到 run 详情，工单里的诊断摘要只能来自记录本身
        # （``snapshot`` 是这一轮的历史留存、属于 View Diagnosis 的展示内容，不塞进工单）。
        #
        # ⚠️ dismiss 在这里是 **best-effort**，与忽略/误报的严格姿态刻意不同：
        # ``reason`` 的取值枚举属于 spike 服务（``aidiag``，**另一个仓**），本仓只能看到
        # 它接受过 ``ignored`` / ``false_positive`` 两个值——``escalated`` 是**猜的**。
        # 猜错不该让"工单已经建出来"这件事回滚，所以失败只记进 ``session_status``
        # （在历史里可见，**不是静默降级**），不抛。
        resp = await _spike_post(request, f"/dismiss/{sid}", {"reason": "escalated"})
        if resp.status_code == 404:
            recorded["session_status"] = "expired"
        elif resp.status_code >= 300:
            recorded["session_status"] = f"dismiss_failed: HTTP {resp.status_code}"
        else:
            recorded["session_status"] = "dismissed"

        existing = _created_ticket(rec)
        if existing is None:
            existing = await _agentflow_create_ticket(request, tenant, rec, None)
        recorded["ticket_id"] = existing.get("ticket_id")
        recorded["ticket_number"] = existing.get("ticket_number")
    else:
        # 忽略 / 误报：签终态。会话过期（404）不阻断关单。
        reason = "ignored" if body.decision == "ignore" else "false_positive"
        resp = await _spike_post(request, f"/dismiss/{sid}", {"reason": reason})
        if resp.status_code == 404:
            recorded["session_status"] = "expired"
        elif resp.status_code >= 300:
            raise AppException(
                ErrorCode.UPSTREAM,
                f"diagnosis dismiss failed: HTTP {resp.status_code} {_resp_detail(resp)}",
            )
        else:
            recorded["session_status"] = "dismissed"

        if body.decision == "false_positive":
            await _record_fpr(storage, tenant, rec, false_positive=True)
            await storage.records.resolve(tenant, record_id, reason=reason)
        else:
            await storage.records.close(tenant, record_id, reason=reason)

    if not await storage.records.append_evidence(tenant, record_id, recorded):
        raise AppException(ErrorCode.CONFLICT, f"problem {record_id} vanished before recording")

    await _apply_escalated_state(storage, tenant, record_id, body.decision, recorded)

    fresh = await storage.records.get(tenant, record_id)
    return {
        "record_id": record_id,
        "decision": body.decision,
        "session_id": sid,
        "session_status": recorded["session_status"],
        "remediation_status": recorded["remediation_status"],
        "reanalyze_count": recorded["reanalyze_count"],
        "max_reanalyze": recorded["max_reanalyze"],
        "ticket_id": recorded["ticket_id"],
        "ticket_number": recorded["ticket_number"],
        "record_state": (fresh or rec).get("state"),
    }


@router.get("/{record_id}/diagnose/decisions")
async def list_problem_diagnosis_decisions(request: Request, record_id: str) -> dict:
    """该问题单的历史计划（被拒/被忽略/被误报的每一版），按写入顺序。只读，无状态守卫。"""
    tenant = get_tenant_id(request)
    rec = await request.app.state.storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")
    return {"items": _diagnose_decisions(rec)}
