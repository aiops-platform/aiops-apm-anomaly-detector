"""问题单存储：``problem_record`` 落库与去重。

- ``RecordStore``（ABC）：M5 emit 与 M6 API 消费的窄接口。
- ``InMemoryRecordStore``：demo/单测真源（UC-2.2/2.3/2.4）。
- ``MySQLRecordStore``：生产实现，``open_group_key`` 生成列 + UNIQUE + ON DUPLICATE KEY UPDATE 原子去重。

每个方法入口校验 ``tenant_id`` 非空（多租户隔离硬约束）。
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from ..models.record import ProblemRecord
from .connection import ConnectionPool, _as_json

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
            rows = [r for r in rows if r["service"] == service]
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
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = self._rows.get(record_id)
        if row is None or row["tenant_id"] != tenant_id:
            return
        row["state"] = "resolved"
        row["resolved_at"] = datetime.now(timezone.utc)
        row["resolve_reason"] = reason

    async def list_tenants(self) -> builtins.list[str]:
        return sorted({r["tenant_id"] for r in self._rows.values()})


def _decode_json(value: Any) -> Any:
    import json

    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


class MySQLRecordStore(RecordStore):
    """MySQL 实现：``open_group_key`` 生成列 + UNIQUE 原子去重。"""

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
            args.append(val)
        placeholders = ", ".join(["%s"] * len(_RECORD_COLUMNS))
        cols = ", ".join(_RECORD_COLUMNS)
        # JSON_MERGE_PRESERVE 把新 evidence 数组按元素拼接到已有 evidence
        args.append(_as_json(record.evidence))
        sql = (
            f"INSERT INTO problem_record ({cols}) VALUES ({placeholders}) "
            "ON DUPLICATE KEY UPDATE "
            "evidence = JSON_MERGE_PRESERVE(IFNULL(evidence, JSON_ARRAY()), CAST(%s AS JSON)), "
            "occurrence_count = occurrence_count + 1, "
            "last_seen_at = VALUES(last_seen_at), "
            "severity = IF(FIELD(VALUES(severity),'warning','high','critical') > "
            "FIELD(severity,'warning','high','critical'), VALUES(severity), severity), "
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
            sql += " AND service=%s"
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
        if not tenant_id:
            raise ValueError("tenant_id is required")
        await self._pool.execute(
            "UPDATE problem_record SET state='resolved', resolved_at=NOW(3), resolve_reason=%s "
            "WHERE tenant_id=%s AND record_id=%s AND state <> 'resolved'",
            (reason, tenant_id, record_id),
        )

    async def list_tenants(self) -> builtins.list[str]:
        rows = await self._pool.fetchall("SELECT DISTINCT tenant_id FROM problem_record ORDER BY tenant_id")
        return [r[0] for r in rows]
