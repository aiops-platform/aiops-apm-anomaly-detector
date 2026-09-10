"""UC-6.4：``/v1/problems`` 问题单查询与手动关闭。M7（UC-7.6）resolve 支持误报回写。"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel

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
    """按租户列问题单，可选 state / service / severity 过滤，detected_at 倒序。"""
    tenant = get_tenant_id(request)
    items = await request.app.state.storage.records.list(
        tenant, state=state, service=service, severity=severity, limit=limit
    )
    return {"items": items}


@router.get("/{record_id}")
async def get_problem(request: Request, record_id: str) -> dict:
    """单条问题单详情。"""
    tenant = get_tenant_id(request)
    rec = await request.app.state.storage.records.get(tenant, record_id)
    if rec is None:
        raise AppException(ErrorCode.NOT_FOUND, f"problem record not found: {record_id}")
    return rec


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

    group_key = rec.get("group_key")
    if group_key:
        await storage.dynamic_config.write_fpr(tenant, group_key, false_positive=false_positive)
        fpr_data = await storage.dynamic_config.load_fpr(tenant)
        update_fpr_gauge(
            tenant,
            rec.get("domain", "application"),
            rec.get("service", "unknown"),
            fpr_data,
        )

    await storage.records.resolve(tenant, record_id, reason="manual")
    return {
        "record_id": record_id,
        "state": "resolved",
        "false_positive_recorded": bool(group_key),
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
