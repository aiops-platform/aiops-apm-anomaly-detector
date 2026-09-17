-- 软删改用独立 deleted 标记（1=已删），不再复用 enabled 字段。
-- 列表/调度只取 deleted=0；DELETE 置 deleted=1，enabled 保持不变。

ALTER TABLE monitor_target ADD COLUMN IF NOT EXISTS deleted SMALLINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_tenant_deleted ON monitor_target (tenant_id, deleted);

COMMENT ON COLUMN monitor_target.deleted IS '软删标记：1=已删，列表/调度只取 0';
