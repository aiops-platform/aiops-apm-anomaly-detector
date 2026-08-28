-- V5：detection_round 目标子表 —— round → 多个 monitor_target 的一对多明细。
-- 每轮（round）每个 target 一行：独立采集状态（running/ok/failed/interrupted）与信号量，
-- 供 round 粒度审计与「上一轮 running 孤儿 → 标记 interrupted → 水位线回退」恢复使用。
-- 与 detection_round 主表通过 round_id 关联（round_id 即 trace_id）。

USE aiops_apm_runtime;

CREATE TABLE IF NOT EXISTS detection_round_target (
    round_id      VARCHAR(64)  NOT NULL COMMENT '归属轮次，即 detection_round.round_id / trace_id',
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    target_id     VARCHAR(64)  NOT NULL COMMENT 'monitor_target.target_id',
    status        VARCHAR(16)  NOT NULL DEFAULT 'running' COMMENT 'running/ok/failed/interrupted',
    signals_count INT          NOT NULL DEFAULT 0 COMMENT '本 target 本轮采集的信号数',
    error         TEXT         DEFAULT NULL COMMENT '失败原因（failed/interrupted 时）',
    started_at    DATETIME(3)  NOT NULL,
    finished_at   DATETIME(3)  DEFAULT NULL,
    created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (round_id, tenant_id, target_id),
    INDEX idx_tenant_target (tenant_id, target_id, finished_at)
) ENGINE=InnoDB;
