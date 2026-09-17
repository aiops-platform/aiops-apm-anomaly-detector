-- M7：detection_round 增加 domain 列（UC-7.2 轮次审计 API 按 domain 过滤）。
-- V1 建表时未含 domain；轮次按 (tenant_id, domain) 分组，审计需按 domain 查询。
-- PG 无列序概念，MySQL 版的 AFTER tenant_id 已去掉。

ALTER TABLE detection_round ADD COLUMN IF NOT EXISTS domain VARCHAR(32) NOT NULL DEFAULT 'application';
