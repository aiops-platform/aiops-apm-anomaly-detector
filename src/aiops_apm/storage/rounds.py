"""检测轮次审计存储：``detection_round`` 表读写（UC-7.2）。

- ``RoundStore``（ABC）：``poller.run_round`` 每轮 create（running）→ ``run_domain`` →
  update_status（success/partial/failed）；``router/audit`` 查询（按 domain/status 过滤）。
- ``InMemoryRoundStore``：单测/demo 真源。
- ``PGRoundStore``：``timeline``/``target_ids``/``degraded_sources`` 存 JSON。
- **round → target 一对多明细**（``detection_round_target``，V5 迁移）：每轮下每个 target
  一行独立采集状态（running/ok/failed/interrupted）与信号量 ——
  ``create_target`` / ``update_target_status`` / ``latest_target`` 三方法。
  供 per-target 审计与「上一轮 running 孤儿 → 标记 interrupted → 水位线回退」恢复。
- **per-target 漏斗计数**（V6 迁移）：``anomaly_count`` / ``record_count`` / ``suppressed_count``，
  按 target.service 归因回填（漏斗在合并信号集上跑、键是 service）。``update_target_status``
  的 status/finished_at 可选 → 采集后与漏斗后两段回填互不覆盖；采集失败的 target 跳过归因。
- **per-target 出站请求参数**（V8 迁移）：``request_params`` JSON 列存本轮采集实际下发的
  ``{method, url, params}``（params 为时间窗口/水位线下推/时区转换后的最终查询参数），
  供审计排查「这轮到底请求了什么」。只存 URL 查询参数，不含 headers（可能带明文凭据）。

每方法入口校验 ``tenant_id`` 非空（多租户隔离硬约束）。
V3 迁移为 ``detection_round`` 补 ``domain`` 列（UC-7.2 审计按 domain 过滤）。
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from datetime import datetime

from .connection import ConnectionPool, _as_json, _decode_json


def _json_safe(value: object) -> object:
    """timeline 里的 datetime 转 isoformat 字符串，保证 ``_as_json`` 可序列化（JSON 列）。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class RoundStore(ABC):
    """检测轮次审计读写接口。"""

    @abstractmethod
    async def create_round(
        self,
        tenant_id: str,
        round_id: str,
        domain: str,
        *,
        started_at: datetime,
        status: str = "running",
        target_ids: list | None = None,
        timeline: list | None = None,
    ) -> None:
        """记录一轮开始（status=running，finished_at 为 NULL）。"""

    @abstractmethod
    async def update_status(
        self,
        tenant_id: str,
        round_id: str,
        status: str,
        *,
        ended_at: datetime,
        timeline: list | None = None,
        signals_count: int | None = None,
        anomaly_count: int | None = None,
        record_count: int | None = None,
        suppressed_count: int | None = None,
        degraded_sources: list | None = None,
    ) -> None:
        """收尾更新轮次状态与计数（success/partial/failed）。"""

    @abstractmethod
    async def get_round(self, tenant_id: str, round_id: str) -> dict | None:
        """按 round_id 取单条轮次；不存在返回 None。"""

    @abstractmethod
    async def list_rounds(
        self,
        tenant_id: str,
        *,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[dict]:
        """按租户查询轮次，可选按 domain/status 过滤，started_at 倒序。"""

    @abstractmethod
    async def create_target(
        self, tenant_id: str, round_id: str, target_id: str, *, started_at: datetime
    ) -> None:
        """记录某轮下某 target 开始采集（status=running，signals_count=0）。"""

    @abstractmethod
    async def update_target_status(
        self,
        tenant_id: str,
        round_id: str,
        target_id: str,
        status: str | None = None,
        *,
        finished_at: datetime | None = None,
        signals_count: int | None = None,
        anomaly_count: int | None = None,
        record_count: int | None = None,
        suppressed_count: int | None = None,
        error: str | None = None,
        request_params: dict | None = None,
    ) -> None:
        """更新某轮下某 target 的采集结果（ok/failed/interrupted）。

        只更新传入的字段（status/finished_at 为 None 时不动，供采集后与漏斗后两段回填）。
        漏斗计数（anomaly/record/suppressed）按 service 归因回填，多 target 共用 service 时共享。
        ``request_params`` 存本轮实际下发的出站请求参数（V8，JSON 列）。
        """

    @abstractmethod
    async def latest_target(
        self, tenant_id: str, target_id: str, *, status: str | None = None
    ) -> dict | None:
        """该 target 最近一次轮次明细（started_at 倒序），可选按 status 过滤；无则 None。

        调度恢复用：下一轮开始前查上一轮是否仍 running（孤儿）→ 标记 interrupted。
        """

    @abstractmethod
    async def list_targets(self, tenant_id: str, round_id: str) -> builtins.list[dict]:
        """某轮下所有 target 明细（按 target_id 排序），审计查询用。"""


class InMemoryRoundStore(RoundStore):
    """内存实现：单测与本地 demo 真源。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict] = {}
        self._target_rows: dict[tuple[str, str, str], dict] = {}

    async def create_round(
        self,
        tenant_id: str,
        round_id: str,
        domain: str,
        *,
        started_at: datetime,
        status: str = "running",
        target_ids: list | None = None,
        timeline: list | None = None,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self._rows[(tenant_id, round_id)] = {
            "round_id": round_id,
            "tenant_id": tenant_id,
            "domain": domain,
            "started_at": started_at,
            "finished_at": None,
            "status": status,
            "target_ids": list(target_ids or []),
            "signals_count": 0,
            "anomaly_count": 0,
            "record_count": 0,
            "suppressed_count": 0,
            "degraded_sources": [],
            "timeline": _json_safe(list(timeline or [])),
        }

    async def update_status(
        self,
        tenant_id: str,
        round_id: str,
        status: str,
        *,
        ended_at: datetime,
        timeline: list | None = None,
        signals_count: int | None = None,
        anomaly_count: int | None = None,
        record_count: int | None = None,
        suppressed_count: int | None = None,
        degraded_sources: list | None = None,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = self._rows.get((tenant_id, round_id))
        if row is None:
            return
        row["status"] = status
        row["finished_at"] = ended_at
        if timeline is not None:
            row["timeline"] = _json_safe(list(timeline))
        if signals_count is not None:
            row["signals_count"] = signals_count
        if anomaly_count is not None:
            row["anomaly_count"] = anomaly_count
        if record_count is not None:
            row["record_count"] = record_count
        if suppressed_count is not None:
            row["suppressed_count"] = suppressed_count
        if degraded_sources is not None:
            row["degraded_sources"] = list(degraded_sources)

    async def get_round(self, tenant_id: str, round_id: str) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = self._rows.get((tenant_id, round_id))
        return dict(row) if row is not None else None

    async def list_rounds(
        self,
        tenant_id: str,
        *,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = [r for r in self._rows.values() if r["tenant_id"] == tenant_id]
        if domain is not None:
            rows = [r for r in rows if r["domain"] == domain]
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        return [dict(r) for r in rows[offset : offset + limit]]

    async def create_target(
        self, tenant_id: str, round_id: str, target_id: str, *, started_at: datetime
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self._target_rows[(tenant_id, round_id, target_id)] = {
            "round_id": round_id,
            "tenant_id": tenant_id,
            "target_id": target_id,
            "status": "running",
            "signals_count": 0,
            "anomaly_count": 0,
            "record_count": 0,
            "suppressed_count": 0,
            "error": None,
            "request_params": None,
            "started_at": started_at,
            "finished_at": None,
        }

    async def update_target_status(
        self,
        tenant_id: str,
        round_id: str,
        target_id: str,
        status: str | None = None,
        *,
        finished_at: datetime | None = None,
        signals_count: int | None = None,
        anomaly_count: int | None = None,
        record_count: int | None = None,
        suppressed_count: int | None = None,
        error: str | None = None,
        request_params: dict | None = None,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = self._target_rows.get((tenant_id, round_id, target_id))
        if row is None:
            return
        if status is not None:
            row["status"] = status
        if finished_at is not None:
            row["finished_at"] = finished_at
        if signals_count is not None:
            row["signals_count"] = signals_count
        if anomaly_count is not None:
            row["anomaly_count"] = anomaly_count
        if record_count is not None:
            row["record_count"] = record_count
        if suppressed_count is not None:
            row["suppressed_count"] = suppressed_count
        if error is not None:
            row["error"] = error
        if request_params is not None:
            row["request_params"] = _json_safe(request_params)

    async def latest_target(
        self, tenant_id: str, target_id: str, *, status: str | None = None
    ) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = [
            r
            for r in self._target_rows.values()
            if r["tenant_id"] == tenant_id and r["target_id"] == target_id
        ]
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        if not rows:
            return None
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        return dict(rows[0])

    async def list_targets(self, tenant_id: str, round_id: str) -> builtins.list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = [
            r
            for r in self._target_rows.values()
            if r["tenant_id"] == tenant_id and r["round_id"] == round_id
        ]
        rows.sort(key=lambda r: r["target_id"])
        return [dict(r) for r in rows]


def _iso(value: datetime) -> str:
    return value.isoformat()


class PGRoundStore(RoundStore):
    """PostgreSQL 实现：JSONB 列用 ``_as_json``/``_decode_json``，单 handle 原则。"""

    _COLUMNS = (
        "round_id", "tenant_id", "domain", "started_at", "finished_at", "status",
        "target_ids", "signals_count", "anomaly_count", "record_count",
        "suppressed_count", "degraded_sources", "timeline",
    )
    _JSON_COLUMNS = {"target_ids", "degraded_sources", "timeline"}

    _TARGET_COLUMNS = (
        "round_id", "tenant_id", "target_id", "status",
        "signals_count", "anomaly_count", "record_count", "suppressed_count",
        "error", "request_params", "started_at", "finished_at",
    )
    _TARGET_JSON_COLUMNS = {"request_params"}

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def _row_to_dict(self, row: tuple) -> dict:
        d: dict = dict(zip(self._COLUMNS, row, strict=True))
        for col in self._JSON_COLUMNS:
            if d.get(col) is not None:
                d[col] = _decode_json(d[col])
        for col in ("started_at", "finished_at"):
            if d.get(col) is not None:
                d[col] = datetime.fromisoformat(str(d[col]))
        return d

    def _target_row_to_dict(self, row: tuple) -> dict:
        d: dict = dict(zip(self._TARGET_COLUMNS, row, strict=True))
        for col in self._TARGET_JSON_COLUMNS:
            if d.get(col) is not None:
                d[col] = _decode_json(d[col])
        for col in ("started_at", "finished_at"):
            if d.get(col) is not None:
                d[col] = datetime.fromisoformat(str(d[col]))
        return d

    async def create_round(
        self,
        tenant_id: str,
        round_id: str,
        domain: str,
        *,
        started_at: datetime,
        status: str = "running",
        target_ids: list | None = None,
        timeline: list | None = None,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        await self._pool.execute(
            "INSERT INTO detection_round (round_id, tenant_id, domain, started_at, status, target_ids, timeline) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                round_id, tenant_id, domain, _iso(started_at), status,
                _as_json(list(target_ids or [])), _as_json(_json_safe(list(timeline or []))),
            ),
        )

    async def update_status(
        self,
        tenant_id: str,
        round_id: str,
        status: str,
        *,
        ended_at: datetime,
        timeline: list | None = None,
        signals_count: int | None = None,
        anomaly_count: int | None = None,
        record_count: int | None = None,
        suppressed_count: int | None = None,
        degraded_sources: list | None = None,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        sets = ["status=%s", "finished_at=%s"]
        args: list = [status, _iso(ended_at)]
        if timeline is not None:
            sets.append("timeline=%s")
            args.append(_as_json(_json_safe(timeline)))
        if signals_count is not None:
            sets.append("signals_count=%s")
            args.append(int(signals_count))
        if anomaly_count is not None:
            sets.append("anomaly_count=%s")
            args.append(int(anomaly_count))
        if record_count is not None:
            sets.append("record_count=%s")
            args.append(int(record_count))
        if suppressed_count is not None:
            sets.append("suppressed_count=%s")
            args.append(int(suppressed_count))
        if degraded_sources is not None:
            sets.append("degraded_sources=%s")
            args.append(_as_json(degraded_sources))
        args.extend([tenant_id, round_id])
        await self._pool.execute(
            f"UPDATE detection_round SET {', '.join(sets)} WHERE tenant_id=%s AND round_id=%s",
            tuple(args),
        )

    async def get_round(self, tenant_id: str, round_id: str) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        cols = ", ".join(self._COLUMNS)
        row = await self._pool.fetchone(
            f"SELECT {cols} FROM detection_round WHERE tenant_id=%s AND round_id=%s",
            (tenant_id, round_id),
        )
        return None if row is None else self._row_to_dict(row)

    async def list_rounds(
        self,
        tenant_id: str,
        *,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        cols = ", ".join(self._COLUMNS)
        sql = f"SELECT {cols} FROM detection_round WHERE tenant_id=%s"
        args: list = [tenant_id]
        if domain is not None:
            sql += " AND domain=%s"
            args.append(domain)
        if status is not None:
            sql += " AND status=%s"
            args.append(status)
        sql += " ORDER BY started_at DESC LIMIT %s OFFSET %s"
        args.extend([int(limit), int(offset)])
        rows = await self._pool.fetchall(sql, tuple(args))
        return [self._row_to_dict(r) for r in rows]

    async def create_target(
        self, tenant_id: str, round_id: str, target_id: str, *, started_at: datetime
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        await self._pool.execute(
            "INSERT INTO detection_round_target (round_id, tenant_id, target_id, status, started_at) "
            "VALUES (%s, %s, %s, 'running', %s)",
            (round_id, tenant_id, target_id, _iso(started_at)),
        )

    async def update_target_status(
        self,
        tenant_id: str,
        round_id: str,
        target_id: str,
        status: str | None = None,
        *,
        finished_at: datetime | None = None,
        signals_count: int | None = None,
        anomaly_count: int | None = None,
        record_count: int | None = None,
        suppressed_count: int | None = None,
        error: str | None = None,
        request_params: dict | None = None,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        sets: list[str] = []
        args: list = []
        if status is not None:
            sets.append("status=%s")
            args.append(status)
        if finished_at is not None:
            sets.append("finished_at=%s")
            args.append(_iso(finished_at))
        if signals_count is not None:
            sets.append("signals_count=%s")
            args.append(int(signals_count))
        if anomaly_count is not None:
            sets.append("anomaly_count=%s")
            args.append(int(anomaly_count))
        if record_count is not None:
            sets.append("record_count=%s")
            args.append(int(record_count))
        if suppressed_count is not None:
            sets.append("suppressed_count=%s")
            args.append(int(suppressed_count))
        if error is not None:
            sets.append("error=%s")
            args.append(error)
        if request_params is not None:
            sets.append("request_params=%s")
            args.append(_as_json(_json_safe(request_params)))
        if not sets:
            return
        args.extend([round_id, tenant_id, target_id])
        await self._pool.execute(
            f"UPDATE detection_round_target SET {', '.join(sets)} "
            "WHERE round_id=%s AND tenant_id=%s AND target_id=%s",
            tuple(args),
        )

    async def latest_target(
        self, tenant_id: str, target_id: str, *, status: str | None = None
    ) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        cols = ", ".join(self._TARGET_COLUMNS)
        sql = (
            f"SELECT {cols} FROM detection_round_target "
            "WHERE tenant_id=%s AND target_id=%s"
        )
        args: list = [tenant_id, target_id]
        if status is not None:
            sql += " AND status=%s"
            args.append(status)
        sql += " ORDER BY started_at DESC LIMIT 1"
        row = await self._pool.fetchone(sql, tuple(args))
        return None if row is None else self._target_row_to_dict(row)

    async def list_targets(self, tenant_id: str, round_id: str) -> builtins.list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        cols = ", ".join(self._TARGET_COLUMNS)
        rows = await self._pool.fetchall(
            f"SELECT {cols} FROM detection_round_target "
            "WHERE tenant_id=%s AND round_id=%s ORDER BY target_id",
            (tenant_id, round_id),
        )
        return [self._target_row_to_dict(r) for r in rows]
