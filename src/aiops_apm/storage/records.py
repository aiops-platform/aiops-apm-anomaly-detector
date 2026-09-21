"""问题单存储：``problem_record`` 落库与去重。

- ``RecordStore``（ABC）：M5 emit 与 M6 API 消费的窄接口。
- ``InMemoryRecordStore``：demo/单测真源（UC-2.2/2.3/2.4）。
- ``PGRecordStore``：生产实现，``open_group_key`` 生成列 + UNIQUE + ON CONFLICT DO UPDATE 原子去重。

每个方法入口校验 ``tenant_id`` 非空（多租户隔离硬约束）。
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from ..models.record import ProblemRecord
from .connection import ConnectionPool, _as_json, _decode_json

_OPEN_STATES = ("pending", "in_progress")
_SEVERITY_RANK = {"warning": 0, "high": 1, "critical": 2}

# problem_record 的标量 + JSON 业务列（不含生成列 open_group_key 与审计列）
_RECORD_COLUMNS = [
    "record_id",
    "group_key",
    "source",
    "tenant_id",
    "domain",
    "state",
    "service",
    "instance",
    "severity",
    "detected_at",
    "first_seen_at",
    "last_seen_at",
    "occurrence_count",
    "resolved_at",
    "resolve_reason",
    "symptom",
    "metric_anomalies",
    "log_anomalies",
    "correlation",
    "change_related",
    "recent_change",
    "verification",
    "evidence",
    "trace_id",
]
_JSON_COLUMNS = {
    "symptom",
    "metric_anomalies",
    "log_anomalies",
    "correlation",
    "recent_change",
    "verification",
    "evidence",
}


class RecordStore(ABC):
    """problem_record 读写/去重接口。"""

    @abstractmethod
    async def find_open(self, tenant_id: str, group_key: str) -> dict | None:
        """租户内同 group_key 的 open 记录（pending/in_progress），无则 None。"""

    @abstractmethod
    async def write_or_append(self, tenant_id: str, record: ProblemRecord) -> None:
        """新开或追加：命中 open 记录只追加 evidence/次数/时间/严重度，不重复开单。"""

    @abstractmethod
    async def list(
        self,
        tenant_id: str,
        *,
        state: str | None = None,
        service: str | None = None,
        severity: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict]:
        """按租户查询，可选按 state/service/severity 过滤，按 detected_at 倒序。"""

    @abstractmethod
    async def get(self, tenant_id: str, record_id: str) -> dict | None:
        """按 record_id 取单条记录；不存在返回 None。"""

    @abstractmethod
    async def resolve(self, tenant_id: str, record_id: str, reason: str = "auto") -> None:
        """关闭记录：state=resolved，open_group_key 自动变 NULL（允许复发开新单）。"""

    @abstractmethod
    async def close(self, tenant_id: str, record_id: str, reason: str = "manual") -> None:
        """关闭记录：state=closed（与 resolved 并列的终态；open_group_key 自动变 NULL，
        允许复发开新单）。

        resolved = "已修复/已处理"，closed = "人判定不做"（忽略）。两者复用同一组审计列
        ``resolved_at``/``resolve_reason``（通用的"关闭时间/原因"，非 resolved 专属）。
        """

    @abstractmethod
    async def mark_escalated(
        self, tenant_id: str, record_id: str, reason: str = "escalated"
    ) -> None:
        """升级终态：state=escalated —— 诊断被人工认可，已派出一张修复工单。

        与 ``resolved``/``closed`` 并列的**第三个终态**，同样复用
        ``resolved_at``/``resolve_reason`` 这组通用审计列。``open_group_key`` 生成列是
        白名单 CASE（``state IN ('pending','in_progress')``），会自动把它排除在 open 之外
        —— 复发照常开新单，与 resolved/closed 一致。

        调用方（``router/problems.py`` 的 escalate 分支）传 ``reason="escalated:<工单号>"``：
        那个值会显示在前端 Detail 弹窗的「Resolve Reason」行上，是**不查 evidence 就能看到
        工单号**的唯一位置。
        """

    @abstractmethod
    async def mark_in_progress(
        self, tenant_id: str, record_id: str, *, run_id: str, workflow_id: str
    ) -> bool:
        """pending → in_progress（仅翻转一次，调用方保证/配合防重入）。

        成功把 agent 分析 run 信息追加进 evidence（``{type:"agent_run", run_id,
        workflow_id, started_at}``）；返回是否真正发生了翻转（记录不存在 / 租户
        不符 / 已不在 pending → False，不抛错）。
        """

    @abstractmethod
    async def append_evidence(self, tenant_id: str, record_id: str, entry: dict) -> bool:
        """向记录 evidence 追加一条条目，不改状态（如 ``{type:"diagnose_session", …}``）。

        返回记录是否存在且租户相符（不存在/不符 → False，不抛错）。
        """

    @abstractmethod
    async def list_tenants(self) -> builtins.list[str]:
        """所有出现过 problem_record 的租户（去重排序），reconcile 扫描用。"""


class InMemoryRecordStore(RecordStore):
    """内存实现：单测与本地 demo 真源。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    async def find_open(self, tenant_id: str, group_key: str) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        for row in self._rows.values():
            if (
                row["tenant_id"] == tenant_id
                and row["group_key"] == group_key
                and row["state"] in _OPEN_STATES
            ):
                return dict(row)
        return None

    async def write_or_append(self, tenant_id: str, record: ProblemRecord) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        for row in self._rows.values():
            if (
                row["tenant_id"] == tenant_id
                and row["group_key"] == record.group_key
                and row["state"] in _OPEN_STATES
            ):
                row["evidence"] = [*row["evidence"], *record.evidence]
                row["occurrence_count"] += 1
                row["last_seen_at"] = record.last_seen_at or record.detected_at
                if _SEVERITY_RANK.get(record.severity, 0) > _SEVERITY_RANK.get(row["severity"], 0):
                    row["severity"] = record.severity
                return
        row = record.model_dump()
        row["group_key"] = record.group_key
        self._rows[record.record_id] = row

    async def list(
        self,
        tenant_id: str,
        *,
        state: str | None = None,
        service: str | None = None,
        severity: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = [r for r in self._rows.values() if r["tenant_id"] == tenant_id]
        if state is not None:
            rows = [r for r in rows if r["state"] == state]
        if service is not None:
            # 成员匹配：M9 起跨服务记录的 service 是逗号拼接串，精确相等会漏掉
            rows = [r for r in rows if service in (r["service"] or "").split(",")]
        if severity is not None:
            rows = [r for r in rows if r["severity"] == severity]
        rows.sort(key=lambda r: r["detected_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    async def get(self, tenant_id: str, record_id: str) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = self._rows.get(record_id)
        if row is None or row["tenant_id"] != tenant_id:
            return None
        return dict(row)

    async def resolve(self, tenant_id: str, record_id: str, reason: str = "auto") -> None:
        self._set_terminal_state(tenant_id, record_id, "resolved", reason)

    async def close(self, tenant_id: str, record_id: str, reason: str = "manual") -> None:
        self._set_terminal_state(tenant_id, record_id, "closed", reason)

    async def mark_escalated(
        self, tenant_id: str, record_id: str, reason: str = "escalated"
    ) -> None:
        self._set_terminal_state(tenant_id, record_id, "escalated", reason)

    def _set_terminal_state(
        self, tenant_id: str, record_id: str, state: str, reason: str
    ) -> None:
        """三个终态（resolved/closed/escalated）的**同一份**落库动作。

        抽出来是防第三份复制：resolve 与 close 原本逐行相同、只差一个状态字符串，
        加 escalate 就是第三份——而"三个终态该写哪几列"是一个**必须保持一致**的决定
        （审计列复用见 ABC 的 ``mark_escalated`` docstring）。
        """
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = self._rows.get(record_id)
        if row is None or row["tenant_id"] != tenant_id:
            return
        row["state"] = state
        row["resolved_at"] = datetime.now(timezone.utc)
        row["resolve_reason"] = reason

    async def mark_in_progress(
        self, tenant_id: str, record_id: str, *, run_id: str, workflow_id: str
    ) -> bool:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = self._rows.get(record_id)
        if row is None or row["tenant_id"] != tenant_id or row["state"] != "pending":
            return False
        row["state"] = "in_progress"
        evidence = row.setdefault("evidence", [])
        if evidence is None:
            evidence = []
            row["evidence"] = evidence
        evidence.append(
            {
                "type": "agent_run",
                "run_id": run_id,
                "workflow_id": workflow_id,
                "started_at": datetime.now(timezone.utc),
            }
        )
        return True

    async def append_evidence(self, tenant_id: str, record_id: str, entry: dict) -> bool:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = self._rows.get(record_id)
        if row is None or row["tenant_id"] != tenant_id:
            return False
        evidence = row.setdefault("evidence", [])
        if evidence is None:
            evidence = []
            row["evidence"] = evidence
        evidence.append(entry)
        return True

    async def list_tenants(self) -> builtins.list[str]:
        return sorted({r["tenant_id"] for r in self._rows.values()})


class PGRecordStore(RecordStore):
    """PostgreSQL 实现：``open_group_key`` 生成列 + UNIQUE 原子去重。"""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def _row_to_dict(self, row: tuple) -> dict[str, Any]:
        d: dict[str, Any] = dict(zip(_RECORD_COLUMNS, row, strict=True))
        for col in _JSON_COLUMNS:
            if d.get(col) is not None:
                d[col] = _decode_json(d[col])
        d["change_related"] = bool(d.get("change_related", False))
        return d

    async def find_open(self, tenant_id: str, group_key: str) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        cols = ", ".join(_RECORD_COLUMNS)
        row = await self._pool.fetchone(
            f"SELECT {cols} FROM problem_record "
            "WHERE tenant_id=%s AND group_key=%s AND state IN ('pending','in_progress') "
            "ORDER BY detected_at DESC LIMIT 1",
            (tenant_id, group_key),
        )
        return None if row is None else self._row_to_dict(row)

    async def write_or_append(self, tenant_id: str, record: ProblemRecord) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        d = record.model_dump()
        d["group_key"] = record.group_key
        args: list[Any] = []
        for col in _RECORD_COLUMNS:
            val = d[col]
            if col in _JSON_COLUMNS:
                val = _as_json(val)
            elif col == "change_related":
                # change_related 是 SMALLINT 列。psycopg3 按 Python 类型选 dumper，bool 会走
                # boolean OID，而 boolean → smallint 没有赋值转换，直接传 True 会在
                # write_or_append 这条热路径上硬报错（MySQL 会静默强转）。
                val = int(bool(val))
            args.append(val)
        placeholders = ", ".join(["%s"] * len(_RECORD_COLUMNS))
        cols = ", ".join(_RECORD_COLUMNS)
        sql = (
            f"INSERT INTO problem_record ({cols}) VALUES ({placeholders}) "
            "ON CONFLICT (tenant_id, open_group_key) DO UPDATE SET "
            # jsonb 的 || 按元素拼接数组，等价于 MySQL 的
            # JSON_MERGE_PRESERVE(IFNULL(evidence, JSON_ARRAY()), new_evidence)。
            # 这里直接取 EXCLUDED.evidence（即上面 VALUES 里绑定的那份），无需再传一个参数。
            "evidence = COALESCE(problem_record.evidence, '[]'::jsonb) || EXCLUDED.evidence, "
            "occurrence_count = problem_record.occurrence_count + 1, "
            "last_seen_at = EXCLUDED.last_seen_at, "
            # FIELD() 在 PG 无对应物。注意 array_position 未命中返回 NULL 而 FIELD 返回 0，
            # 必须 COALESCE 成 0：当「新严重度在词表内、旧值不在」时（例如旧值被运维写成
            # 'medium'），MySQL 会升级到新值，而 NULL 参与 > 比较得 NULL → 走 ELSE → 保留
            # 旧值，判定就悄悄变了。
            "severity = CASE WHEN "
            "COALESCE(array_position(ARRAY['warning','high','critical'], EXCLUDED.severity), 0) > "
            "COALESCE(array_position(ARRAY['warning','high','critical'], problem_record.severity), 0) "
            "THEN EXCLUDED.severity ELSE problem_record.severity END, "
            "updated_at = CURRENT_TIMESTAMP(3)"
        )
        await self._pool.execute(sql, tuple(args))

    async def list(
        self,
        tenant_id: str,
        *,
        state: str | None = None,
        service: str | None = None,
        severity: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        cols = ", ".join(_RECORD_COLUMNS)
        sql = f"SELECT {cols} FROM problem_record WHERE tenant_id=%s"
        args: list[Any] = [tenant_id]
        if state is not None:
            sql += " AND state=%s"
            args.append(state)
        if service is not None:
            # 成员匹配而非精确相等：M9 起跨服务记录的 service 是逗号拼接串
            # （如 "gateway-service,order-service"），`service=%s` 会漏掉这类记录。
            # 用 string_to_array 做精确成员判定，不用 LIKE —— LIKE '%x%' 会误匹配
            # 子串（"order" 命中 "order-service"）。
            sql += " AND %s = ANY(string_to_array(service, ','))"
            args.append(service)
        if severity is not None:
            sql += " AND severity=%s"
            args.append(severity)
        sql += " ORDER BY detected_at DESC LIMIT %s"
        args.append(int(limit))
        rows = await self._pool.fetchall(sql, tuple(args))
        return [self._row_to_dict(r) for r in rows]

    async def get(self, tenant_id: str, record_id: str) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        cols = ", ".join(_RECORD_COLUMNS)
        row = await self._pool.fetchone(
            f"SELECT {cols} FROM problem_record WHERE tenant_id=%s AND record_id=%s",
            (tenant_id, record_id),
        )
        return None if row is None else self._row_to_dict(row)

    async def resolve(self, tenant_id: str, record_id: str, reason: str = "auto") -> None:
        await self._set_terminal_state(tenant_id, record_id, "resolved", reason)

    async def close(self, tenant_id: str, record_id: str, reason: str = "manual") -> None:
        await self._set_terminal_state(tenant_id, record_id, "closed", reason)

    async def mark_escalated(
        self, tenant_id: str, record_id: str, reason: str = "escalated"
    ) -> None:
        await self._set_terminal_state(tenant_id, record_id, "escalated", reason)

    async def _set_terminal_state(
        self, tenant_id: str, record_id: str, state: str, reason: str
    ) -> None:
        """三个终态（resolved/closed/escalated）的**同一份**落库动作。

        抽出来是防第三份复制：resolve 与 close 原本只差一个状态字符串，加 escalate 就是
        第三份——而"三个终态该写哪几列、守卫怎么写"是一个**必须保持一致**的决定。

        守卫 ``state <> %s`` 保持逐字语义：目标状态是自身时不重复写（并发/重试幂等），
        但**不阻止**从别的终态转过来（resolved → escalated 是允许的，反向也是）——
        这是改动前就有的形状（`/resolve`、`/ignore` 两个端点不检查当前 state）。
        """
        if not tenant_id:
            raise ValueError("tenant_id is required")
        await self._pool.execute(
            "UPDATE problem_record SET state=%s, resolved_at=CURRENT_TIMESTAMP(3), resolve_reason=%s "
            "WHERE tenant_id=%s AND record_id=%s AND state <> %s",
            (state, reason, tenant_id, record_id, state),
        )

    async def mark_in_progress(
        self, tenant_id: str, record_id: str, *, run_id: str, workflow_id: str
    ) -> bool:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        entry = {
            "type": "agent_run",
            "run_id": run_id,
            "workflow_id": workflow_id,
            "started_at": datetime.now(timezone.utc),
        }
        # WHERE state='pending' + execute_affected(rowcount)：原子单翻，并发双提交只有一方成功。
        affected = await self._pool.execute_affected(
            "UPDATE problem_record SET state='in_progress', "
            "evidence = COALESCE(evidence, '[]'::jsonb) || jsonb_build_array(%s::jsonb) "
            "WHERE tenant_id=%s AND record_id=%s AND state='pending'",
            (_as_json(entry), tenant_id, record_id),
        )
        return affected == 1

    async def append_evidence(self, tenant_id: str, record_id: str, entry: dict) -> bool:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        affected = await self._pool.execute_affected(
            "UPDATE problem_record SET "
            "evidence = COALESCE(evidence, '[]'::jsonb) || jsonb_build_array(%s::jsonb) "
            "WHERE tenant_id=%s AND record_id=%s",
            (_as_json(entry), tenant_id, record_id),
        )
        return affected == 1

    async def list_tenants(self) -> builtins.list[str]:
        rows = await self._pool.fetchall("SELECT DISTINCT tenant_id FROM problem_record ORDER BY tenant_id")
        return [r[0] for r in rows]
