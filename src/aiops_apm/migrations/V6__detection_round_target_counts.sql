-- V6：detection_round_target 补 per-target 漏斗计数列。
-- V5 建表时只有 signals_count（采集量）；L0-L3 在合并信号集上跑、按 service 归因，
-- 因此把 anomaly_count / record_count / suppressed_count 按 target.service 匹配回填到每行，
-- 供「哪个 target 的 service 触发了异常/开单/被抑制」的审计。多 target 共用 service 时共享计数。

ALTER TABLE detection_round_target
    ADD COLUMN IF NOT EXISTS anomaly_count    INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS record_count     INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS suppressed_count INT NOT NULL DEFAULT 0;
