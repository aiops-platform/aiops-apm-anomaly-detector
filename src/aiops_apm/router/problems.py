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

import json
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..collectors._gateway import OutboundGateway
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


class AnalyzeProblemBody(BaseModel):
    workflow_id: str


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
    return {"items": [_strip_decision_snapshots(r) for r in items]}


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
    return rec


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
    """手动关闭问题单（reason=manual）。

    M7（UC-7.6）：可选 body ``{"false_positive": true}``——为真时把该单 ``group_key``
    记为一次误报，写回 ``fpr_table``（total+1，fpr 重算）并更新 ``aiops_false_positive_rate`` Gauge。
    body 缺省 / 为假 → 记为一次有效判定（非误报）。
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
    cmdb_ci: dict = {"name": rec.get("service") or ""}
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
    }
    req_id = _first_log_chain_id(rec)
    if req_id:
        # git-search 工作流入参：app-log-analyst 按它查链路日志（无则 require 预检判失败）
        ticket["requestId"] = req_id
    return ticket


@router.post("/{record_id}/analyze")
async def analyze_problem(request: Request, record_id: str, body: AnalyzeProblemBody) -> dict:
    """对 pending 问题发起 agent 工作流分析（→ Bug Solve / agentflow run）。

    顺序：1) 校验 workflow_id → 2) 记录存在性（404）→ 3) state=pending 守卫（否则 409）→
    4) 组平铺 ticket → 5) POST agentflow ``/run``（失败/非 2xx → 502，绝不翻转状态）→
    6) 成功才 ``mark_in_progress``（WHERE state='pending' 原子单翻），evidence 记 run_id。
    """
    workflow_id = (body.workflow_id or "").strip()
    if not workflow_id:
        raise AppException(ErrorCode.VALIDATION, "workflow_id is required")

    tenant = get_tenant_id(request)
    storage = request.app.state.storage
    rec = await storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")
    if (rec.get("state") or "pending") != "pending":
        raise AppException(
            ErrorCode.CONFLICT,
            f"problem {record_id} is not pending (state={rec.get('state')})",
        )

    ticket = _build_ticket(rec)
    base = str(request.app.state.settings.bug_solve_base_url).rstrip("/")
    url = base + "/run"
    # 出站安全网关：base 为 operator 配置地址；回环仅当 APM_ALLOW_LOOPBACK=true 放行（本地 .env 已设）。
    OutboundGateway.validate_url(url)
    http = request.app.state.http_client
    try:
        resp = await http.request("POST", url, json={"workflow_id": workflow_id, "ticket": ticket})
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

    flipped = await storage.records.mark_in_progress(
        tenant, record_id, run_id=run_id, workflow_id=workflow_id
    )
    if not flipped:
        # 并发场景：run 已启动，但记录已被移出 pending（如他处 resolve）
        raise AppException(ErrorCode.CONFLICT, f"problem {record_id} was concurrently moved out of pending")

    return {"record_id": record_id, "state": "in_progress", "run_id": run_id}


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

    - app：``record.service``（服务身份即日志源）；
    - repo：请求覆盖 > ``settings.diagnose_repo`` > ``record.service``（spike 侧 repo 即仓库定位）；
    - trace_id：请求覆盖 > evidence 里首条业务链路 ID（``_first_log_chain_id``）；
    - log_excerpt：请求覆盖 > 首条日志异常签名。
    """
    payload: dict = {
        "app": body.app or rec.get("service") or "",
        "repo": body.repo or settings.diagnose_repo or rec.get("service"),
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
    if (rec.get("state") or "pending") in ("resolved", "closed", "archived"):
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
    """读该问题单绑定的诊断会话（``GET {diagnose_base_url}/status/{session_id}``）原文。

    未绑定诊断 → 404；上游失败/超时 → 502。
    """
    tenant = get_tenant_id(request)
    storage = request.app.state.storage
    settings = request.app.state.settings
    rec = await storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")
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
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        raise AppException(ErrorCode.UPSTREAM, "diagnosis status returned invalid body") from exc


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
    decision: Literal["reject", "ignore", "false_positive"]
    feedback: str = Field("", max_length=2000)  # reject 必填非空；忽略/误报忽略之
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
    """给快照上体积护栏，保证单次病态长输出撑不爆 MySQL JSON 行。

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
    if (rec.get("state") or "pending") in ("resolved", "closed", "archived"):
        raise AppException(
            ErrorCode.CONFLICT,
            f"problem {record_id} is {rec.get('state')}; decision not allowed",
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

    fresh = await storage.records.get(tenant, record_id)
    return {
        "record_id": record_id,
        "decision": body.decision,
        "session_id": sid,
        "session_status": recorded["session_status"],
        "remediation_status": recorded["remediation_status"],
        "reanalyze_count": recorded["reanalyze_count"],
        "max_reanalyze": recorded["max_reanalyze"],
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
