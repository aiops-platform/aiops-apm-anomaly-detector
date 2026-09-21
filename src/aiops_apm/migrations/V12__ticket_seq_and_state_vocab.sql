-- V12：「升级」派出的修复工单取号表 + problem_record.state 词表补 escalated。
--
-- 背景：Problem Center 的「View diagnosis → 处理」新增第三种裁定**升级**——
-- 认可诊断 → 在 agentflow 建一张修复工单 → 工单号绑回本记录 → state 转 escalated 终态。
-- 工单号由本仓生成（`SequenceStore.next_ticket_number`），格式沿用 PR- 的日期分段约定，
-- 前缀改 INC-。
--
-- 1) ticket_seq：**不能复用 record_seq**。它的主键是 seq_date（单列），两种号共用同一个
--    计数器会互相跳号——PR-20260921-0007 与 INC-20260921-0007 并存，且两边都有空洞，
--    排查时像丢号。表结构与 record_seq 逐字对齐，取号 SQL 也照抄
--    （INSERT ... ON CONFLICT DO UPDATE ... RETURNING，见 storage/sequence.py）。
--
-- 2) state 词表：`COMMENT ON COLUMN` 是**唯一的权威词表文档**（列是 VARCHAR(16)，
--    没有 CHECK、没有 PG enum，加值不需要 DDL）。V1 里那份写在建表时，已应用的迁移
--    不会重跑，只能在这里覆盖一次。
--
--    ⚠️ 不动 open_group_key 生成列：它的 CASE 是白名单 `state IN ('pending','in_progress')`，
--    escalated 是终态、天然被排除在 open 之外（⇒ 复发自动开新单，与 resolved/closed 一致）。
--    要改那条表达式得 DROP + ADD 生成列并重建唯一索引——**没有必要**。
--
-- 注意：这里**不写** SET search_path —— schema 由 MigrationRunner 按 settings.db_schema
-- 注入（tests/test_migrations.py::test_scripts_do_not_hardcode_schema 守着这条）。

CREATE TABLE IF NOT EXISTS ticket_seq (
    seq_date  VARCHAR(8)  NOT NULL PRIMARY KEY,
    next_seq  BIGINT      NOT NULL DEFAULT 1
);

COMMENT ON COLUMN ticket_seq.seq_date IS 'YYYYMMDD';

COMMENT ON COLUMN problem_record.state IS 'pending/in_progress/resolved/closed/archived/escalated';
