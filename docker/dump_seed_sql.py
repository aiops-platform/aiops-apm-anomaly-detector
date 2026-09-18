"""把活库 ``aiops_apm_runtime`` 的当前数据导出成 **V11 种子迁移**（一次性快照工具）。

用法::

    python docker/dump_seed_sql.py
    python docker/dump_seed_sql.py --snapshot-json /tmp/seed.json   # 另存冻结快照供校验

产出 ``src/aiops_apm/migrations/V11__seed_live_data.sql``，随 ``make migrate`` 自动生效。

为什么需要这个脚本：V11 是**某一时刻活库的快照**，内容会随活库变化；要重新生成得靠它，
不能手写。V9 的先例是「脚本权威、迁移是首次快照」，这里同理——但 V11 的真源是**活库**，
本脚本是取快照的工具。**V11 一旦合并即不可变**（同 V9 约定），后续刷新走新迁移。

--- 取快照的两个硬要求 -------------------------------------------------------

1. **只读**：连接串带 ``default_transaction_read_only=on``。渲染器写错也不可能改到生产库。
2. **一致快照**：所有查询跑在**同一个 REPEATABLE READ 事务**里。活库的 scheduler 一直在写
   （实测 10 分钟内 signal_snapshot 从 1834 涨到 1846），不隔离就会跨时间点读到
   ``detection_round`` 与其子表 ``detection_round_target``，种出孤儿明细。
   注意用的是 ``autocommit=True`` + 显式 ``BEGIN``：psycopg 非 autocommit 下首条语句前
   已隐式 BEGIN，此时再发 ``BEGIN ISOLATION LEVEL ...`` **只是 WARNING、隔离级别静默不生效**。

--- 渲染规则（都是踩过的坑）--------------------------------------------------

- **按声明类型渲染，不按 Python 类型**：jsonb 标量经 psycopg 解码后也是 ``str``，与
  VARCHAR 不可区分。列类型从系统目录取。
- **jsonb ``null`` 与 SQL NULL 必须区分**：psycopg 把两者都解码成 ``None``。实测
  ``problem_record.recent_change`` 有 2 行确实是 jsonb ``'null'``，故对 jsonb 列额外查一个
  ``(col IS NULL)`` 标志位。否则要么把真 NULL 写成 ``'null'::jsonb``，要么反之。
- **不产 ``E'...'``**：``runner._split_statements`` 认不出 E-string 里的 ``\\'``，
  一个奇数个撇号会把**整个文件**吞成一条语句。一律用 ``''`` 双写转义。
- **datetime 必须 naive**：``TIMESTAMP(3)`` 列会**静默忽略**字面量里的时区偏移，
  aware 值会带来不报错的偏移。这里直接断言。
- **身下列**从 ``attgenerated = ''`` 取，自动排除 ``problem_record.open_group_key`` 生成列，
  且将来加列也不会漏。
- **写文件无 BOM、``\\n`` 换行**：``runner._load_scripts`` 用 ``read_text(encoding="utf-8")``，
  BOM 会让第一行注释被当成 SQL；注释里若再有撇号就会翻转引号状态、吞掉整个文件。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from aiops_apm.settings import Settings

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/aiops_apm/migrations/V11__seed_live_data.sql"

# 每块 INSERT 的行数。块越大往返越少，但单条语句出错会拖垮整个迁移事务（runner 全程一个事务）。
CHUNK_ROWS = 200


@dataclass
class TableSpec:
    """一张表的取数规格。``order_by`` 必须确定，否则两次 dump 的 diff 无法审阅。"""

    table: str
    order_by: str
    limit: int | None = None
    note: str = ""


# 要种入的表。顺序即输出顺序。
SEEDED: list[TableSpec] = [
    TableSpec("domain_config", "id"),
    TableSpec("fpr_table", "id"),
    TableSpec("record_seq", "seq_date"),
    TableSpec("problem_record", "record_id"),
    TableSpec("detection_state", "tenant_id, domain, state_key"),
    TableSpec("collect_watermark", "tenant_id, target_id"),
    TableSpec("detection_round", "round_id"),
    TableSpec("detection_round_target", "round_id, tenant_id, target_id"),
    TableSpec(
        "signal_snapshot",
        "snapshot_ts DESC, id DESC",
        limit=20,
        note="**只种样本**：全表 ~1843 行、约占全部字节的 1/3，且装的是原始生产日志正文"
        "（含堆栈与 traceId）。该表在 src/ 里**只写不读**（无任何 SELECT），种全量既无功能"
        "收益，又会让生产日志永久留在 git 历史里。此处只取最新 20 行做形状样本。",
    ),
]

# 不种入的表 —— 原因必须留在生成物里，否则下一个人会以为是漏了。
EXCLUDED: dict[str, str] = {
    "monitor_target": "由 V9 拥有（V9 < V11，种了也永远输给 V9 的 ON CONFLICT DO NOTHING，"
    "结果状态完全一样）。且它的 source_config 内嵌 ES 地址，而 V9 刻意走 "
    "aiops.testbed_es_url GUC 注入以保证环境可移植。改这三个端点用 make seed-testbed。",
    "scheduler_lease": "是锁不是数据。storage/lease.py 的接管判断是 expires_at < now："
    "种入固定时间戳若落在新环境启动之后，scheduler 会一直抢不到锁；种成 NULL 则 CASE 取不到"
    "真值走 ELSE 保留原 holder——**永久**抢不到。缺行时是纯 INSERT 直接成功，不种才是正确初始态。",
    "schema_versions": "由 MigrationRunner 自己管理，种进去会破坏版本追踪。",
    "change_record": "活库为空。",
    "maintenance_window": "活库为空。",
    "suppress_blacklist": "活库为空。",
}

# 类型分发用的 typname（pg_type.typname）→ 渲染器
_INT_TYPES = {"int2", "int4", "int8"}
_FLOAT_TYPES = {"float4", "float8"}


def _quote(text: str) -> str:
    """单引号字面量：内层撇号双写。

    刻意不产生 ``E'...'``：runner 的切分器不认 E-string 的 ``\\'`` 转义，
    一个奇数撇号会把整个文件吞成一条语句。
    """
    if "\x00" in text:
        raise ValueError("数据含 NUL 字节，PostgreSQL 的 text 类型存不下")
    return "'" + text.replace("'", "''") + "'"


def render(value: object, typname: str, *, sql_is_null: bool) -> str:
    """把单个值渲染成 SQL 字面量。

    ``sql_is_null`` 是 ``(col IS NULL)`` 的真实结果——仅 jsonb 列需要它来区分
    「SQL NULL」与「jsonb ``null``」（两者经 psycopg 都是 ``None``）。
    """
    if typname == "jsonb":
        if sql_is_null:
            return "NULL"
        return _quote(json.dumps(value, ensure_ascii=False)) + "::jsonb"

    if sql_is_null or value is None:
        return "NULL"

    if typname in _INT_TYPES:
        return str(value)  # SMALLINT 标志位必须是整数：PG 没有 boolean→smallint 赋值转换
    if typname in _FLOAT_TYPES:
        return repr(value)  # repr 是最短往返表示；inf/nan 渲染成裸字面量，PG 接受
    if typname == "numeric":
        return str(value)
    if typname == "bool":
        return "TRUE" if value else "FALSE"
    if typname.startswith("timestamp"):
        assert isinstance(value, datetime), f"{typname} 期望 datetime，得到 {type(value)}"
        assert value.tzinfo is None, (
            f"timestamp 列的值带时区（{value!r}）——TIMESTAMP(3) 会静默忽略偏移，写入即偏移"
        )
        return "TIMESTAMP " + _quote(value.isoformat(sep=" ", timespec="milliseconds"))
    if isinstance(value, (str,)):
        return _quote(value)
    # Decimal 之外的未知类型：交给 str()，但显式报出来以免静默走错分支
    raise TypeError(f"未处理的列类型 {typname}（值类型 {type(value).__name__}）")


@dataclass
class Column:
    name: str
    typname: str
    is_identity: bool


def read_columns(cur: psycopg.Cursor, table: str) -> list[Column]:
    """从系统目录读列清单。``attgenerated = ''`` 排除生成列（open_group_key）。"""
    cur.execute(
        """
        SELECT a.attname, t.typname, a.attidentity
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_type t ON t.oid = a.atttypid
        WHERE n.nspname = current_schema()
          AND c.relname = %s
          AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = ''
        ORDER BY a.attnum
        """,
        (table,),
    )
    return [Column(name=r[0], typname=r[1], is_identity=r[2] != "") for r in cur.fetchall()]


def fetch_rows(cur: psycopg.Cursor, spec: TableSpec, columns: list[Column]) -> list[list[str]]:
    """取数并渲染成字面量矩阵。jsonb 列额外查 ``IS NULL`` 标志位。"""
    # 先排全部值列，再把 jsonb 的 IS NULL 标志位统一追加在**末尾**——不能交错，
    # 否则下面的切片对不上（psycopg 是按 SELECT 顺序给元组的）。
    select_items: list[str] = [f'"{c.name}"' for c in columns]
    for col in columns:
        if col.typname == "jsonb":
            select_items.append(f'("{col.name}" IS NULL) AS "__null__{col.name}"')

    sql = f'SELECT {", ".join(select_items)} FROM "{spec.table}" ORDER BY {spec.order_by}'
    if spec.limit is not None:
        sql += f" LIMIT {spec.limit}"
    cur.execute(sql)

    rows: list[list[str]] = []
    for raw in cur.fetchall():
        values = raw[: len(columns)]
        flags_iter = iter(raw[len(columns) :])
        rendered: list[str] = []
        for col, value in zip(columns, values, strict=True):
            is_null = bool(next(flags_iter)) if col.typname == "jsonb" else value is None
            rendered.append(render(value, col.typname, sql_is_null=is_null))
        rows.append(rendered)
    return rows


@dataclass
class DumpedTable:
    spec: TableSpec
    columns: list[Column]
    rows: list[list[str]] = field(default_factory=list)

    @property
    def identity_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.is_identity]


def emit_table_sql(dumped: DumpedTable) -> str:
    """渲染一张表的 INSERT 语句（分块 + 裸 ON CONFLICT DO NOTHING）。"""
    col_list = ", ".join(f'"{c.name}"' for c in dumped.columns)
    parts: list[str] = []
    for start in range(0, len(dumped.rows), CHUNK_ROWS):
        chunk = dumped.rows[start : start + CHUNK_ROWS]
        values = ",\n".join("    (" + ", ".join(row) + ")" for row in chunk)
        # 裸 ON CONFLICT（不指定冲突目标）：problem_record 可能撞主键**或** uk_open_group_key，
        # 写死目标会漏掉另一种、直接抛 23505 而不是跳过。
        parts.append(f'INSERT INTO "{dumped.spec.table}" ({col_list}) VALUES\n{values}\nON CONFLICT DO NOTHING;')
    return "\n\n".join(parts)


def emit_setval_sql(table: str, column: str) -> str:
    """推进 identity 序列。

    必须用 DO 块包住并显式 RAISE：``setval`` 是 **STRICT** 函数，第一个参数为 NULL 时
    它**静默什么都不做、也不报错**——而 ``pg_get_serial_sequence`` 找不到序列时正是返回 NULL。
    那样会得到一个"成功"的迁移和下一次采集的主键撞车（storage/snapshots.py 与
    monitor_targets.py 的 INSERT 都不写 id，全靠序列）。
    """
    return (
        "DO $$\n"
        "DECLARE seq text;\n"
        "BEGIN\n"
        f"    seq := pg_get_serial_sequence('{table}', '{column}');\n"
        "    IF seq IS NULL THEN\n"
        f"        RAISE EXCEPTION 'V11: {table}.{column} 的 identity 序列不存在，setval 会静默失效';\n"
        "    END IF;\n"
        f"    PERFORM setval(seq, COALESCE((SELECT MAX(\"{column}\") FROM \"{table}\"), 0) + 1, false);\n"
        "END $$;"
    )


def build_header(dumped: list[DumpedTable], settings: Settings, generated_at: str) -> str:
    lines: list[str] = []
    lines.append("-- V11：把活库当前数据固化为初始化数据（跟随 make migrate 自动生效）。")
    lines.append("--")
    lines.append("-- ⚠️ 本文件由 ``docker/dump_seed_sql.py`` 生成，**不要手改**。")
    lines.append(f"-- 生成时间：{generated_at}")
    lines.append(f"-- 来源：{settings.db_name}.{settings.db_schema}（活库快照，非构造数据）")
    lines.append("-- 幂等：全部 INSERT 走裸 ON CONFLICT DO NOTHING——迁移不该覆盖目标库已有的行。")
    lines.append("--")
    lines.append("-- 种入内容：")
    for d in dumped:
        extra = f" —— {d.spec.note}" if d.spec.note else ""
        lines.append(f"--   {d.spec.table:24s} {len(d.rows):5d} 行   ORDER BY {d.spec.order_by}{extra}")
    lines.append("--")
    lines.append("-- 刻意**不种**的表：")
    for table, reason in EXCLUDED.items():
        lines.append(f"--   {table}：{reason}")
    lines.append("--")
    lines.append("-- 种入后新环境的三个行为后果（都是实测确认的，不是推测）：")
    lines.append("--   1. domain_config 一旦有行，domains.yaml 的首次 seed 就不再触发")
    lines.append("--      （config/loader.py 判空才 seed）。新环境跑的是活库配置：")
    lines.append("--      verify.persistence_rounds 已被改成 1，而不是 YAML 里的 2。")
    lines.append("--   2. 种入的两张在办问题单会在启动后几十秒内被 reconcile 自动关掉：")
    lines.append("--      判定条件是「该单所有 anomaly_key 的 miss_rounds >= resolve_after_rounds(3)」，")
    lines.append("--      而种子 detection_state 的 miss_rounds 已是 310/403（state_key 正是这两张单的")
    lines.append("--      anomaly_key）。设 APM_ENABLE_RECONCILER=false 可保留，本地 .env 就是这么设的。")
    lines.append("--   3. collect_watermark 会让新环境首轮请求一个较大的补采窗口（水位线锚在本机时钟上）。")
    lines.append("--      若水位线超前于新环境时钟，collectors/_window.py 的 watermark_is_future()")
    lines.append("--      会跳过下推、走全量重采自愈，所以不会永久卡死，但首轮代价要知道。")
    lines.append("--")
    lines.append("-- 不可变性：V11 一旦合并即视为不可变历史（同 V9 约定）。活库数据会继续变，")
    lines.append("-- 后续刷新走新迁移或 docker/ 下的脚本，**不要改本文件**。")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="导出活库数据为 V11 种子迁移")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"输出路径（默认 {DEFAULT_OUT}）")
    parser.add_argument("--snapshot-json", type=Path, default=None, help="另存冻结快照（供逐行校验用）")
    args = parser.parse_args()

    settings = Settings()
    conninfo = make_conninfo(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        dbname=settings.db_name,
        connect_timeout=5,
    )
    # default_transaction_read_only：渲染器有 bug 也改不到生产库。
    # autocommit=True：见模块 docstring——非 autocommit 下显式 BEGIN ISOLATION LEVEL 是静默无效的。
    options = f"-c search_path={settings.db_schema} -c TimeZone=UTC -c default_transaction_read_only=on"

    with psycopg.connect(conninfo, options=options, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            try:
                generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                dumped: list[DumpedTable] = []
                for spec in SEEDED:
                    columns = read_columns(cur, spec.table)
                    if not columns:
                        raise RuntimeError(f"表 {spec.table} 在 {settings.db_schema} 里不存在")
                    rows = fetch_rows(cur, spec, columns)
                    dumped.append(DumpedTable(spec=spec, columns=columns, rows=rows))
                    note = "（空表，无 INSERT）" if not rows else ""
                    print(f"[dump] {spec.table:24s} {len(rows):5d} 行 {note}")
            finally:
                cur.execute("COMMIT")

    # 组装文件：头注释 → 每表 INSERT → 各表 setval
    sections: list[str] = [build_header(dumped, settings, generated_at)]
    for d in dumped:
        if not d.rows:
            continue
        sections.append(f'-- ---- {d.spec.table} ({len(d.rows)} 行) ----')
        sections.append(emit_table_sql(d))

    setvals: list[str] = []
    for d in dumped:
        for col in d.identity_columns:
            setvals.append(emit_setval_sql(d.spec.table, col))
    if setvals:
        sections.append(
            "-- ---- identity 序列推进 ----\n"
            "-- 上面的 INSERT 显式写入了 id，但 identity 序列不会因此前进；不 setval 的话\n"
            "-- 下一次真实写入（storage/snapshots.py、monitor_targets.py 都不写 id）会撞主键。\n"
            "-- 按目标表的 MAX(id)+1 计算，所以表里已有更大 id 时也安全。\n\n" + "\n\n".join(setvals)
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # encoding="utf-8" 且不写 BOM；newline="\n" 保证跨平台一致（runner 按 utf-8 读，BOM 会坏事）
    args.out.write_text("\n\n".join(sections) + "\n", encoding="utf-8", newline="\n")
    size_kb = args.out.stat().st_size / 1024
    print(f"[dump] 写入 {args.out} ({size_kb:.0f} KB)")

    if args.snapshot_json is not None:
        snapshot = {
            "generated_at": generated_at,
            "schema": settings.db_schema,
            "tables": {
                d.spec.table: {
                    "columns": [c.name for c in d.columns],
                    "order_by": d.spec.order_by,
                    "rows": d.rows,
                }
                for d in dumped
            },
        }
        args.snapshot_json.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
        )
        print(f"[dump] 冻结快照 -> {args.snapshot_json}")


if __name__ == "__main__":
    main()
