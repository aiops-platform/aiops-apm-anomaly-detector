-- 软删改用独立 deleted 标记（1=已删），不再复用 enabled 字段。
-- 列表/调度只取 deleted=0；DELETE 置 deleted=1，enabled 保持不变。
ALTER TABLE monitor_target ADD COLUMN deleted TINYINT(1) NOT NULL DEFAULT 0 AFTER enabled;
ALTER TABLE monitor_target ADD INDEX idx_tenant_deleted (tenant_id, deleted);
