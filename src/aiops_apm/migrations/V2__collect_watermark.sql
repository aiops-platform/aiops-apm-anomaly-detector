-- V2: 采集水位线表（M3 采集层）
-- 记录每个 monitor_target 最近一次采集到的事件时间戳，用于下轮下推时间窗实现增量采集。
-- 单租户单目标一行；主键 (tenant_id, target_id) 保证不重复。
-- 时间戳存 naive UTC（见 collectors/_window.py 的水位线约定）。

CREATE TABLE IF NOT EXISTS collect_watermark (
    tenant_id   VARCHAR(64) NOT NULL DEFAULT 'default',
    target_id   VARCHAR(32) NOT NULL,
    last_ts     TIMESTAMP(3) NOT NULL,
    updated_at  TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (tenant_id, target_id)
);

COMMENT ON TABLE collect_watermark IS '采集水位线：每个监控端点最近采集到的事件时间戳';

DROP TRIGGER IF EXISTS trg_collect_watermark_updated_at ON collect_watermark;
CREATE TRIGGER trg_collect_watermark_updated_at BEFORE UPDATE ON collect_watermark
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
