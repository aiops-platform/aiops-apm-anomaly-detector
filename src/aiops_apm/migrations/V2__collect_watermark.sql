-- V2: 采集水位线表（M3 采集层）
-- 记录每个 monitor_target 最近一次采集到的事件时间戳，用于下轮下推时间窗实现增量采集。
-- 单租户单目标一行；主键 (tenant_id, target_id) 保证不重复。
CREATE TABLE IF NOT EXISTS collect_watermark (
    tenant_id   VARCHAR(64) NOT NULL DEFAULT 'default',
    target_id   VARCHAR(32) NOT NULL,
    last_ts     DATETIME(3) NOT NULL,
    updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (tenant_id, target_id)
) ENGINE=InnoDB COMMENT='采集水位线：每个监控端点最近采集到的事件时间戳';
