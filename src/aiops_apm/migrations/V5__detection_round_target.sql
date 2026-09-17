-- V5：detection_round 目标子表 —— round → 多个 monitor_target 的一对多明细。
-- 每轮（round）每个 target 一行：独立采集状态（running/ok/failed/interrupted）与信号量，
-- 供 round 粒度审计与「上一轮 running 孤儿 → 标记 interrupted → 水位线回退」恢复使用。
-- 与 detection_round 主表通过 round_id 关联（round_id 即 trace_id）。

CREATE TABLE IF NOT EXISTS detection_round_target (
    round_id      VARCHAR(64)  NOT NULL,
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default',
    target_id     VARCHAR(64)  NOT NULL,
    status        VARCHAR(16)  NOT NULL DEFAULT 'running',
    signals_count INT          NOT NULL DEFAULT 0,
    error         TEXT         DEFAULT NULL,
    started_at    TIMESTAMP(3) NOT NULL,
    finished_at   TIMESTAMP(3) DEFAULT NULL,
    created_at    TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (round_id, tenant_id, target_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_target ON detection_round_target (tenant_id, target_id, finished_at);

COMMENT ON COLUMN detection_round_target.round_id IS '归属轮次，即 detection_round.round_id / trace_id';
COMMENT ON COLUMN detection_round_target.tenant_id IS '多租户隔离';
COMMENT ON COLUMN detection_round_target.target_id IS 'monitor_target.target_id';
COMMENT ON COLUMN detection_round_target.status IS 'running/ok/failed/interrupted';
COMMENT ON COLUMN detection_round_target.signals_count IS '本 target 本轮采集的信号数';
COMMENT ON COLUMN detection_round_target.error IS '失败原因（failed/interrupted 时）';
